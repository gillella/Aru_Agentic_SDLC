import json
import os
import stat
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import slack_projects  # noqa: E402
from slack_projects import (  # noqa: E402
    ProjectRegistry,
    RegistryError,
    record_seen_event,
    secure_mutate_json,
)


def identity(path: Path):
    suffix = path.name.replace("-", "_")
    return {
        "github_repo_id": f"R_{suffix}",
        "github_repo_database_id": len(suffix),
        "project_v2_id": f"PVT_{suffix}",
        "repo_slug": f"owner/{suffix}",
        "local_path": str(path.resolve()),
    }


class SlackProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.registry_path = self.root / "projects.json"
        self.audit_path = self.root / "audit.json"
        self.checkout_a = self.root / "project-a"
        self.checkout_b = self.root / "project-b"
        self.checkout_a.mkdir()
        self.checkout_b.mkdir()
        self.registry = ProjectRegistry(self.registry_path, self.audit_path, identity)

    def tearDown(self):
        self.temp.cleanup()

    def create_a(self):
        return self.registry.create(
            self.checkout_a, "T01234567", "C01234567", "operator",
            "proj_project_a",
        )

    def test_create_records_immutable_identity_and_private_files(self):
        record = self.create_a()
        self.assertEqual(record.github_repo_id, "R_project_a")
        self.assertEqual(record.project_v2_id, "PVT_project_a")
        self.assertEqual(record.lifecycle, "active")
        self.assertEqual(stat.S_IMODE(self.registry_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.registry_path.parent.stat().st_mode), 0o700)
        persisted = self.registry_path.read_text(encoding="utf-8")
        self.assertNotIn("xoxb-", persisted)
        self.assertNotIn("token", persisted.lower())

    def test_default_identity_discovers_exact_repo_and_governed_board(self):
        repo = {"id": "R_node", "databaseId": 42, "nameWithOwner": "owner/repo"}
        with patch.object(slack_projects, "run_cmd", return_value=(0, json.dumps(repo), "")), \
             patch.object(
                 slack_projects, "resolve_governed_project", return_value={"id": "PVT_board"}
             ) as resolve:
            found = slack_projects.discover_checkout_identity(self.checkout_a)
        resolve.assert_called_once_with("owner/repo")
        self.assertEqual(found["github_repo_id"], "R_node")
        self.assertEqual(found["github_repo_database_id"], 42)
        self.assertEqual(found["project_v2_id"], "PVT_board")

    def test_active_route_resolves_exactly_one_project(self):
        record = self.create_a()
        resolved = self.registry.resolve("T01234567", "C01234567")
        self.assertEqual(resolved.project_id, record.project_id)
        with self.assertRaises(RegistryError):
            self.registry.resolve("T01234567", "C99999999")

    def test_closed_route_fails_closed_and_cannot_be_rebound(self):
        record = self.create_a()
        closed = self.registry.close(record.project_id, "operator-2")
        self.assertEqual(closed.lifecycle, "closed")
        self.assertIsNotNone(closed.closed_at)
        with self.assertRaises(RegistryError):
            self.registry.resolve("T01234567", "C01234567")
        with self.assertRaisesRegex(RegistryError, "reserved"):
            self.registry.create(
                self.checkout_b, "T01234567", "C01234567", "operator",
                "proj_project_b",
            )

    def test_duplicate_repository_project_is_rejected(self):
        self.create_a()
        with self.assertRaisesRegex(RegistryError, "already has an active binding"):
            self.registry.create(
                self.checkout_a, "T01234567", "C11111111", "operator",
                "proj_duplicate",
            )

    def test_missing_checkout_is_degraded_without_affecting_peer(self):
        first = self.create_a()
        second = self.registry.create(
            self.checkout_b, "T01234567", "C11111111", "operator",
            "proj_project_b",
        )
        self.checkout_a.rmdir()
        self.assertEqual(self.registry.verify(first.project_id)["runtime_health"], "degraded_unreachable")
        self.assertEqual(self.registry.verify(second.project_id)["runtime_health"], "healthy")
        self.assertEqual(
            self.registry.resolve("T01234567", "C11111111").project_id,
            second.project_id,
        )

    def test_recovery_updates_only_mutable_pointers_after_identity_check(self):
        record = self.create_a()
        moved = self.root / "project-a-renamed"
        self.checkout_a.rename(moved)

        def renamed_identity(path: Path):
            value = identity(self.checkout_a)
            value.update({"repo_slug": "owner/new-name", "local_path": str(path.resolve())})
            return value

        registry = ProjectRegistry(self.registry_path, self.audit_path, renamed_identity)
        recovered = registry.recover(record.project_id, "operator-2", moved, "owner/new-name")
        self.assertEqual(recovered.local_path, str(moved.resolve()))
        self.assertEqual(recovered.repo_slug, "owner/new-name")
        self.assertEqual(recovered.github_repo_id, record.github_repo_id)
        self.assertEqual(recovered.project_v2_id, record.project_v2_id)

    def test_recovery_rejects_identity_mismatch_without_mutation(self):
        record = self.create_a()
        before = self.registry_path.read_bytes()

        def wrong(path: Path):
            value = identity(path)
            value["github_repo_id"] = "R_wrong"
            return value

        registry = ProjectRegistry(self.registry_path, self.audit_path, wrong)
        with self.assertRaisesRegex(RegistryError, "different GitHub"):
            registry.recover(record.project_id, "operator", self.checkout_b)
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_corrupt_registry_fails_without_overwriting_recoverable_copy(self):
        self.registry_path.write_text("{not-json", encoding="utf-8")
        self.registry_path.chmod(0o600)
        before = self.registry_path.read_bytes()
        with self.assertRaises(RegistryError):
            self.registry.list()
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_insecure_registry_permissions_fail_closed(self):
        self.create_a()
        self.registry_path.chmod(0o644)
        with self.assertRaisesRegex(RegistryError, "private"):
            self.registry.list()

    def test_registry_symlink_fails_closed_without_replacing_target(self):
        target = self.root / "recoverable.json"
        target.write_text("recoverable", encoding="utf-8")
        target.chmod(0o600)
        self.registry_path.symlink_to(target)
        with self.assertRaisesRegex(RegistryError, "symlink"):
            self.create_a()
        self.assertTrue(self.registry_path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "recoverable")

    def test_migration_is_idempotent_and_never_copies_credentials(self):
        values = {
            "SLACK_TEAM_ID": "T01234567",
            "SLACK_CHANNEL_ID": "C01234567",
            "SLACK_BOT_TOKEN": "xoxb-" + "secret" * 10,
            "SLACK_SIGNING_SECRET": "top-secret",
        }
        first = self.registry.migrate_legacy(values, self.checkout_a, "operator")
        second = self.registry.migrate_legacy(values, self.checkout_a, "operator")
        self.assertEqual(first.project_id, second.project_id)
        self.assertEqual(len(self.registry.list()), 1)
        persisted = self.registry_path.read_text(encoding="utf-8")
        self.assertNotIn(values["SLACK_BOT_TOKEN"], persisted)
        self.assertNotIn(values["SLACK_SIGNING_SECRET"], persisted)

    def test_lock_protected_updates_do_not_lose_projects(self):
        def create(path: Path, channel: str, project_id: str):
            return ProjectRegistry(self.registry_path, self.audit_path, identity).create(
                path, "T01234567", channel, "operator", project_id
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(create, self.checkout_a, "C01234567", "proj_project_a"),
                executor.submit(create, self.checkout_b, "C11111111", "proj_project_b"),
            ]
            for future in futures:
                future.result()
        self.assertEqual({item.project_id for item in self.registry.list()}, {
            "proj_project_a", "proj_project_b",
        })

    def test_audit_throttles_repeated_invalid_routes(self):
        first = self.registry.audit(
            "invalid_inbound_route", "bridge", detail="team=T channel=C",
            throttle_key="T:C", throttle_seconds=60,
        )
        second = self.registry.audit(
            "invalid_inbound_route", "bridge", detail="team=T channel=C",
            throttle_key="T:C", throttle_seconds=60,
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(json.loads(self.audit_path.read_text())["events"]), 1)

    def test_seen_events_are_scoped_by_project_and_route(self):
        seen = self.root / "seen.json"
        self.assertFalse(record_seen_event("proj_one", "T1", "C1", "evt", seen))
        self.assertTrue(record_seen_event("proj_one", "T1", "C1", "evt", seen))
        self.assertFalse(record_seen_event("proj_two", "T1", "C1", "evt", seen))
        self.assertFalse(record_seen_event("proj_one", "T1", "C2", "evt", seen))

    def test_secure_persistence_rejects_credential_shaped_values(self):
        path = self.root / "state.json"
        with self.assertRaisesRegex(RegistryError, "credential-shaped"):
            secure_mutate_json(
                path, {}, lambda _value: {"text": "Bearer do-not-persist"}
            )
        self.assertFalse(path.exists())

    def test_checkout_discovery_resolves_board_from_repo_slug(self):
        repo_json = json.dumps({
            "databaseId": 42, "id": "R_node", "nameWithOwner": "owner/repo",
        })
        with patch.object(slack_projects, "run_cmd", return_value=(0, repo_json, "")), \
             patch.object(
                 slack_projects, "resolve_governed_project",
                 return_value={"id": "PVT_board"},
             ) as resolve:
            found = slack_projects.discover_checkout_identity(self.checkout_a)
        resolve.assert_called_once_with("owner/repo")
        self.assertEqual(found["github_repo_id"], "R_node")
        self.assertEqual(found["project_v2_id"], "PVT_board")

    def test_checkout_discovery_fails_when_board_is_ambiguous(self):
        repo_json = json.dumps({"id": "R_node", "nameWithOwner": "owner/repo"})
        with patch.object(slack_projects, "run_cmd", return_value=(0, repo_json, "")), \
             patch.object(slack_projects, "resolve_governed_project", return_value=None):
            with self.assertRaisesRegex(RegistryError, "cannot resolve one governed"):
                slack_projects.discover_checkout_identity(self.checkout_a)


if __name__ == "__main__":
    unittest.main()
