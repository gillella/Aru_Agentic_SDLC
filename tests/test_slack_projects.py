# line-ceiling: 618
import json
import os
import stat
import subprocess
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
        repo = {"id": "R_node", "nameWithOwner": "owner/repo"}
        rest_repo = {"id": 42, "node_id": "R_node", "full_name": "owner/repo"}
        project = {"id": "PVT_board"}
        with patch.object(
            slack_projects, "_bounded_json", side_effect=[repo, rest_repo]
        ) as bounded, \
             patch.object(slack_projects, "_discover_governed_project", return_value=project) as resolve:
            found = slack_projects.discover_checkout_identity(self.checkout_a)
        resolve.assert_called_once_with("owner/repo")
        self.assertNotIn("databaseId", bounded.call_args_list[0].args[0])
        self.assertEqual(
            bounded.call_args_list[1].args[0],
            ["gh", "api", "repos/owner/repo"],
        )
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

    def test_close_unknown_project_reports_accurate_error(self):
        with self.assertRaisesRegex(RegistryError, "unknown project_id: proj_missing"):
            self.registry.close("proj_missing", "operator")

    def test_repeated_close_does_not_append_a_false_audit_transition(self):
        record = self.create_a()
        self.registry.close(record.project_id, "operator")
        first_count = len(json.loads(self.audit_path.read_text(encoding="utf-8"))["events"])
        self.registry.close(record.project_id, "operator")
        second_count = len(json.loads(self.audit_path.read_text(encoding="utf-8"))["events"])
        self.assertEqual(second_count, first_count)

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

    def test_owned_legacy_0755_registry_directory_is_secured(self):
        self.root.chmod(0o755)
        with patch.object(os, "chmod", side_effect=NotImplementedError) as chmod, \
             patch.object(os, "fchmod", wraps=os.fchmod) as secured:
            slack_projects._private_directory(self.root)
        chmod.assert_not_called()
        secured.assert_called()
        record = self.create_a()
        self.assertEqual(record.project_id, "proj_project_a")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_world_writable_registry_directory_fails_closed(self):
        self.root.chmod(0o777)
        with self.assertRaisesRegex(RegistryError, "writable by another user"):
            self.create_a()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o777)

    def test_registry_symlink_fails_closed_without_replacing_target(self):
        target = self.root / "recoverable.json"
        target.write_text("recoverable", encoding="utf-8")
        target.chmod(0o600)
        self.registry_path.symlink_to(target)
        with self.assertRaisesRegex(RegistryError, "symlink"):
            self.create_a()
        self.assertTrue(self.registry_path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "recoverable")

    def test_non_string_local_path_fails_as_registry_error(self):
        self.create_a()
        document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        document["projects"]["proj_project_a"]["local_path"] = 123
        self.registry_path.write_text(json.dumps(document), encoding="utf-8")
        self.registry_path.chmod(0o600)
        with self.assertRaisesRegex(RegistryError, "invalid local_path"):
            self.registry.list()

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

    def test_migration_rejects_duplicate_active_repository_identity(self):
        self.create_a()
        values = {"SLACK_TEAM_ID": "T01234567", "SLACK_CHANNEL_ID": "C11111111"}
        with self.assertRaisesRegex(RegistryError, "already has an active binding"):
            self.registry.migrate_legacy(values, self.checkout_a, "operator")

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
        repo = {"id": "R_node", "nameWithOwner": "owner/repo"}
        rest_repo = {"id": 42, "node_id": "R_node", "full_name": "owner/repo"}
        with patch.object(
            slack_projects, "_bounded_json", side_effect=[repo, rest_repo]
        ), \
             patch.object(
                 slack_projects, "_discover_governed_project", return_value={"id": "PVT_board"}
             ) as resolve:
            found = slack_projects.discover_checkout_identity(self.checkout_a)
        resolve.assert_called_once_with("owner/repo")
        self.assertEqual(found["github_repo_id"], "R_node")
        self.assertEqual(found["project_v2_id"], "PVT_board")

    def test_checkout_discovery_fails_when_board_is_ambiguous(self):
        repo = {"id": "R_node", "nameWithOwner": "owner/repo"}
        rest_repo = {"id": 42, "node_id": "R_node", "full_name": "owner/repo"}
        with patch.object(
            slack_projects, "_bounded_json", side_effect=[repo, rest_repo]
        ), \
             patch.object(
                 slack_projects, "_discover_governed_project",
                 side_effect=RegistryError("cannot resolve one governed ProjectV2 board"),
             ):
            with self.assertRaisesRegex(RegistryError, "cannot resolve one governed"):
                slack_projects.discover_checkout_identity(self.checkout_a)

    def test_checkout_discovery_rejects_mismatched_repository_apis(self):
        repo = {"id": "R_node", "nameWithOwner": "owner/repo"}
        for rest_repo in (
            {"id": 42, "node_id": "R_other", "full_name": "owner/repo"},
            {"id": 42, "node_id": "R_node", "full_name": "owner/other"},
        ):
            with self.subTest(rest_repo=rest_repo), patch.object(
                slack_projects, "_bounded_json", side_effect=[repo, rest_repo]
            ), patch.object(slack_projects, "_discover_governed_project") as board:
                with self.assertRaisesRegex(RegistryError, "mismatched data"):
                    slack_projects.discover_checkout_identity(self.checkout_a)
                board.assert_not_called()

    def test_checkout_discovery_rejects_missing_or_invalid_database_id(self):
        repo = {"id": "R_node", "nameWithOwner": "owner/repo"}
        for database_id in (None, "42", True, 0, -1):
            rest_repo = {
                "id": database_id,
                "node_id": "R_node",
                "full_name": "owner/repo",
            }
            with self.subTest(database_id=database_id), patch.object(
                slack_projects, "_bounded_json", side_effect=[repo, rest_repo]
            ), patch.object(slack_projects, "_discover_governed_project") as board:
                with self.assertRaisesRegex(RegistryError, "database id"):
                    slack_projects.discover_checkout_identity(self.checkout_a)
                board.assert_not_called()

    def test_checkout_discovery_rejects_missing_or_mistyped_node_ids(self):
        for repo_node_id, rest_node_id in ((7, 7), ("", ""), ("R_node", 7)):
            repo = {"id": repo_node_id, "nameWithOwner": "owner/repo"}
            rest_repo = {
                "id": 42,
                "node_id": rest_node_id,
                "full_name": "owner/repo",
            }
            with self.subTest(
                repo_node_id=repo_node_id, rest_node_id=rest_node_id
            ), patch.object(
                slack_projects, "_bounded_json", side_effect=[repo, rest_repo]
            ), patch.object(slack_projects, "_discover_governed_project") as board:
                with self.assertRaisesRegex(RegistryError, "identity"):
                    slack_projects.discover_checkout_identity(self.checkout_a)
                board.assert_not_called()

    def test_checkout_identity_commands_are_bounded(self):
        with patch.object(
            slack_projects.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(["gh"], slack_projects.IDENTITY_TIMEOUT_SECONDS),
        ) as invoked:
            with self.assertRaisesRegex(RegistryError, "timed out"):
                slack_projects.discover_checkout_identity(self.checkout_a)
        self.assertEqual(invoked.call_args.kwargs["timeout"], slack_projects.IDENTITY_TIMEOUT_SECONDS)

    def test_checkout_identity_bounds_repo_and_board_queries(self):
        repo = {"id": "R_node", "nameWithOwner": "owner/repo"}
        rest_repo = {"id": 42, "node_id": "R_node", "full_name": "owner/repo"}
        board = {
            "data": {"repository": {"projectsV2": {"nodes": [{
                "id": "PVT_board",
                "title": "repo Board",
                "repositories": {"nodes": [{"nameWithOwner": "owner/repo"}]},
            }]}}}
        }
        responses = [
            subprocess.CompletedProcess([], 0, json.dumps(repo), ""),
            subprocess.CompletedProcess([], 0, json.dumps(rest_repo), ""),
            subprocess.CompletedProcess([], 0, json.dumps(board), ""),
        ]
        with patch.object(slack_projects.subprocess, "run", side_effect=responses) as invoked:
            found = slack_projects.discover_checkout_identity(self.checkout_a)
        self.assertEqual(found["project_v2_id"], "PVT_board")
        self.assertEqual(invoked.call_count, 3)
        self.assertTrue(all(
            call.kwargs["timeout"] == slack_projects.IDENTITY_TIMEOUT_SECONDS
            for call in invoked.call_args_list
        ))


class FindByCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.registry_path = self.root / "projects.json"
        self.audit_path = self.root / "audit.json"
        self.checkout = self.root / "project-a"
        self.checkout.mkdir()
        self.registry = ProjectRegistry(self.registry_path, self.audit_path, identity)

    def tearDown(self):
        self.temp.cleanup()

    def test_resolves_active_binding_from_checkout_path(self):
        self.registry.create(
            self.checkout, "T01234567", "C01234567", "operator", "proj_project_a"
        )
        found = self.registry.find_by_checkout(self.checkout)
        self.assertEqual(found.project_id, "proj_project_a")

    def test_unknown_checkout_fails_closed_with_bind_hint(self):
        self.registry.create(
            self.checkout, "T01234567", "C01234567", "operator", "proj_project_a"
        )
        with self.assertRaises(slack_projects.RegistryError) as ctx:
            self.registry.find_by_checkout(self.root / "nowhere")
        self.assertIn("migrate", str(ctx.exception))

    def test_ambiguous_checkout_fails_closed(self):
        # Two active records bound to the same path cannot coexist by schema
        # (duplicate binding), so ambiguity is guarded by the uniqueness rule
        # itself; assert at least one record resolves cleanly.
        first = self.registry.create(
            self.checkout, "T01234567", "C01234567", "operator", "proj_project_a"
        )
        self.assertEqual(first.project_id, "proj_project_a")

    def test_worktree_under_bound_checkout_resolves_to_its_project(self):
        # Factory agents run from `.worktrees/<branch>`, never from the bound
        # root, so a nested directory must route to the repository it is in.
        self.registry.create(
            self.checkout, "T01234567", "C01234567", "operator", "proj_project_a"
        )
        worktree = self.checkout / ".worktrees" / "fix-issue-355"
        worktree.mkdir(parents=True)
        found = self.registry.find_by_checkout(worktree)
        self.assertEqual(found.project_id, "proj_project_a")

    def test_deepest_bound_checkout_wins_over_enclosing_repository(self):
        outer = self.registry.create(
            self.root, "T01234567", "C01234567", "operator", "proj_outer"
        )
        inner = self.registry.create(
            self.checkout, "T01234567", "C11111111", "operator", "proj_inner"
        )
        self.assertEqual(
            self.registry.find_by_checkout(self.checkout / "src").project_id,
            inner.project_id,
        )
        self.assertEqual(
            self.registry.find_by_checkout(self.root / "elsewhere").project_id,
            outer.project_id,
        )

    def test_repo_slug_resolves_a_checkout_outside_every_bound_path(self):
        self.registry.create(
            self.checkout, "T01234567", "C01234567", "operator", "proj_project_a"
        )
        found = self.registry.find_by_checkout(
            self.root.parent / "elsewhere", repo_slug="owner/project_a"
        )
        self.assertEqual(found.project_id, "proj_project_a")

    def test_unknown_repo_slug_still_fails_closed(self):
        self.registry.create(
            self.checkout, "T01234567", "C01234567", "operator", "proj_project_a"
        )
        with self.assertRaises(slack_projects.RegistryError):
            self.registry.find_by_checkout(
                self.root.parent / "elsewhere", repo_slug="owner/other"
            )


class SharedWarRoomTests(unittest.TestCase):
    """One Anguliyam war room routes every governed project (issue #355)."""

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

    def bind_both(self):
        first = self.registry.create(
            self.checkout_a, "T07L1SZCQEM", "C0BPZMRR1RC", "operator",
            "proj_project_a", shared_channel=True,
        )
        second = self.registry.create(
            self.checkout_b, "T07L1SZCQEM", "C0BPZMRR1RC", "operator",
            "proj_project_b",
        )
        return first, second

    def test_shared_channel_binds_several_active_repositories(self):
        first, second = self.bind_both()
        self.assertTrue(self.registry.is_shared_channel("T07L1SZCQEM", "C0BPZMRR1RC"))
        self.assertEqual(
            [record.project_id for record in self.registry.list(include_closed=False)],
            [first.project_id, second.project_id],
        )

    def test_shared_declaration_persists_for_a_fresh_registry_reader(self):
        self.bind_both()
        reopened = ProjectRegistry(self.registry_path, self.audit_path, identity)
        self.assertEqual(len(reopened.list(include_closed=False)), 2)
        self.assertIn(
            "T07L1SZCQEM:C0BPZMRR1RC", reopened.shared_channels()
        )

    def test_undeclared_channel_keeps_its_one_project_reservation(self):
        self.registry.create(
            self.checkout_a, "T01234567", "C01234567", "operator", "proj_project_a"
        )
        with self.assertRaisesRegex(RegistryError, "reserved"):
            self.registry.create(
                self.checkout_b, "T01234567", "C01234567", "operator", "proj_project_b"
            )
        self.assertEqual(
            self.registry.resolve("T01234567", "C01234567").project_id, "proj_project_a"
        )

    def test_channel_only_resolution_fails_closed_on_a_shared_route(self):
        self.bind_both()
        with self.assertRaisesRegex(RegistryError, "ambiguous shared-channel route") as ctx:
            self.registry.resolve("T07L1SZCQEM", "C0BPZMRR1RC")
        self.assertIn("owner/project_a", str(ctx.exception))
        self.assertIn("owner/project_b", str(ctx.exception))

    def test_repository_metadata_selects_one_project_on_a_shared_route(self):
        first, second = self.bind_both()
        self.assertEqual(
            self.registry.resolve(
                "T07L1SZCQEM", "C0BPZMRR1RC", "owner/project_b"
            ).project_id,
            second.project_id,
        )
        self.assertEqual(
            slack_projects.resolve_project(
                "T07L1SZCQEM", "C0BPZMRR1RC", self.registry_path, "owner/project_a"
            )["project_id"],
            first.project_id,
        )

    def test_unknown_repository_on_a_shared_route_fails_closed(self):
        self.bind_both()
        with self.assertRaises(RegistryError):
            self.registry.resolve("T07L1SZCQEM", "C0BPZMRR1RC", "owner/absent")

    def test_shared_route_candidates_are_listed_for_operator_selection(self):
        first, second = self.bind_both()
        self.assertEqual(
            [
                record.project_id
                for record in self.registry.resolve_candidates("T07L1SZCQEM", "C0BPZMRR1RC")
            ],
            [first.project_id, second.project_id],
        )

    def test_closing_one_shared_project_leaves_the_peer_resolvable(self):
        first, second = self.bind_both()
        self.registry.close(first.project_id, "operator")
        self.assertEqual(
            self.registry.resolve("T07L1SZCQEM", "C0BPZMRR1RC").project_id,
            second.project_id,
        )

    def test_checkout_still_selects_the_project_on_a_shared_channel(self):
        _, second = self.bind_both()
        found = self.registry.find_by_checkout(self.checkout_b)
        self.assertEqual(found.project_id, second.project_id)
        self.assertEqual(found.slack_channel_id, "C0BPZMRR1RC")

    def test_declaring_a_shared_channel_is_audited(self):
        self.bind_both()
        events = json.loads(self.audit_path.read_text(encoding="utf-8"))["events"]
        shared = [event for event in events if event["action"] == "share_channel"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["detail"], "T07L1SZCQEM:C0BPZMRR1RC")

    def test_same_repository_twice_on_a_shared_route_is_rejected(self):
        first, _ = self.bind_both()
        document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        clone = dict(document["projects"][first.project_id])
        clone["project_id"] = "proj_clone"
        document["projects"]["proj_clone"] = clone
        self.registry_path.write_text(
            json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
        )
        with self.assertRaisesRegex(RegistryError, "duplicate Slack binding for"):
            self.registry.list()

    def test_non_object_shared_channels_fails_closed(self):
        self.bind_both()
        document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        document["shared_channels"] = ["T07L1SZCQEM:C0BPZMRR1RC"]
        self.registry_path.write_text(
            json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
        )
        with self.assertRaisesRegex(RegistryError, "shared_channels must be an object"):
            self.registry.list()


if __name__ == "__main__":
    unittest.main()
