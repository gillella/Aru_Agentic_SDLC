import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import slack_control_room as scr  # noqa: E402
from slack_notify import SlackConfig  # noqa: E402
from slack_projects import ProjectRegistry  # noqa: E402


def sample_config(**overrides):
    values = {
        "bot_token": "xoxb-" + ("a" * 45),
        "team_id": "T01234567",
        "channel_id": "",
        "operator_user_id": "U01234567",
        "app_token": "xapp-" + ("b" * 20),
    }
    values.update(overrides)
    return SlackConfig(**values)


class SlackControlRoomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.checkout_a = self.root / "checkout-a"
        self.checkout_b = self.root / "checkout-b"
        self.checkout_a.mkdir()
        self.checkout_b.mkdir()
        self.registry_path = self.root / "projects.json"
        self.audit_path = self.root / "audit.json"
        self.seen_path = self.root / "seen.json"
        self.stop_path = self.root / "factory-loop.stop"

        def identity(path):
            suffix = path.name
            return {
                "github_repo_id": f"R_{suffix}",
                "github_repo_database_id": 1,
                "project_v2_id": f"P_{suffix}",
                "repo_slug": f"owner/{suffix}",
                "local_path": str(path.resolve()),
            }

        self.registry = ProjectRegistry(self.registry_path, self.audit_path, identity)
        self.project_a = self.registry.create(
            self.checkout_a, "T01234567", "C01234567", "operator", "proj_checkout_a"
        )
        self.project_b = self.registry.create(
            self.checkout_b, "T01234567", "C11111111", "operator", "proj_checkout_b"
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def payload(channel="C01234567", event_id="evt-1", text="status", user="U01234567"):
        return {
            "team": "T01234567",
            "channel": channel,
            "user": user,
            "client_msg_id": event_id,
            "text": text,
        }

    def test_parse_commands_default_to_resolved_project(self):
        self.assertEqual(scr.parse_command("<@U999> stop")["target"], "project")
        self.assertEqual(scr.parse_command("resume cursor-1")["target"], "cursor-1")
        self.assertEqual(scr.parse_command("stop all")["target"], "all")
        intervention = scr.parse_command("intervention PR #77 use option B")
        self.assertEqual(intervention["kind"], "pr")
        self.assertEqual(intervention["ref"], "77")
        self.assertEqual(intervention["decision"], "use option B")

    def test_authorization_requires_operator_and_project_workspace(self):
        self.assertTrue(scr.authorize(sample_config(), self.project_a, "U01234567"))
        self.assertFalse(scr.authorize(sample_config(), self.project_a, "U99999999"))
        self.assertFalse(scr.authorize(sample_config(team_id="T99999999"), self.project_a, "U01234567"))
        self.assertFalse(scr.authorize(sample_config(operator_user_id=""), self.project_a, "U01234567"))

    def test_unknown_channel_fails_closed_before_dedupe_or_callbacks(self):
        calls = []
        reply = scr.handle_slack_message(
            sample_config(), self.registry, self.payload(channel="C99999999"), set(),
            comment=lambda *args: calls.append(("comment", args)) or True,
            notify=lambda *args, **kwargs: calls.append(("notify", args)) or {"ok": True},
            seen_path=self.seen_path,
        )
        self.assertIsNone(reply)
        self.assertEqual(calls, [])
        self.assertFalse(self.seen_path.exists())
        events = json.loads(self.audit_path.read_text(encoding="utf-8"))["events"]
        self.assertEqual(events[-1]["action"], "invalid_inbound_route")

    def test_invalid_route_audit_redacts_credential_shaped_input(self):
        credential = "xoxb-" + ("sensitive" * 5)
        payload = self.payload(channel="C99999999")
        payload["team"] = credential
        self.assertIsNone(
            scr.handle_slack_message(
                sample_config(), self.registry, payload, set(), seen_path=self.seen_path
            )
        )
        persisted = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(credential, persisted)
        self.assertIn("[redacted]", persisted)

    def test_closed_channel_fails_closed_with_no_reply(self):
        self.registry.close(self.project_a.project_id, "operator")
        calls = []
        reply = scr.handle_slack_message(
            sample_config(), self.registry, self.payload(), set(),
            notify=lambda *args, **kwargs: calls.append(args) or {"ok": True},
            seen_path=self.seen_path,
        )
        self.assertIsNone(reply)
        self.assertEqual(calls, [])
        self.assertFalse(self.seen_path.exists())

    def test_unauthorized_message_is_ignored_without_recording_event(self):
        reply = scr.handle_slack_message(
            sample_config(), self.registry, self.payload(user="U99999999"), set(),
            seen_path=self.seen_path,
        )
        self.assertIsNone(reply)
        self.assertFalse(self.seen_path.exists())

    def test_dedupe_survives_restart_and_is_scoped_by_project_route(self):
        replies = []
        with patch.object(
            scr, "handle_command",
            side_effect=lambda _c, _p, project, _cb, _health: project.project_id,
        ):
            first = scr.handle_slack_message(
                sample_config(), self.registry, self.payload(), set(),
                notify=lambda config, event: replies.append((config.channel_id, event["project_id"])) or {"ok": True},
                seen_path=self.seen_path,
            )
            duplicate = scr.handle_slack_message(
                sample_config(), self.registry, self.payload(), set(),
                notify=lambda *args: {"ok": True}, seen_path=self.seen_path,
            )
            peer = scr.handle_slack_message(
                sample_config(), self.registry,
                self.payload(channel="C11111111", event_id="evt-1"), set(),
                notify=lambda config, event: replies.append((config.channel_id, event["project_id"])) or {"ok": True},
                seen_path=self.seen_path,
            )
        self.assertEqual(first, "proj_checkout_a")
        self.assertIsNone(duplicate)
        self.assertEqual(peer, "proj_checkout_b")
        self.assertEqual(replies, [
            ("C01234567", "proj_checkout_a"),
            ("C11111111", "proj_checkout_b"),
        ])

    def test_stop_and_resume_use_only_canonical_checkout_path(self):
        with patch.object(scr, "STOP_PATH", self.stop_path):
            stopped = scr.handle_command(
                sample_config(), {"verb": "stop", "target": "project"}, self.project_a
            )
            document = json.loads(self.stop_path.read_text(encoding="utf-8"))
            self.assertIn(str(self.checkout_a.resolve()), document["projects"])
            self.assertNotIn(str(self.checkout_b.resolve()), document["projects"])
            resumed = scr.handle_command(
                sample_config(), {"verb": "resume", "target": "project"}, self.project_a
            )
            self.assertIn("stop recorded", stopped)
            self.assertIn("cleared", resumed)
            self.assertNotIn(str(self.checkout_a.resolve()), json.loads(self.stop_path.read_text())["projects"])

    def test_legacy_0644_stop_file_is_adopted_without_schema_breakage(self):
        self.stop_path.write_text(
            json.dumps({
                "projects": [self.project_a.local_path],
                "stopped_at": "2026-08-14T00:00:00Z",
                "source": "install_local_agent_integrations.sh",
            }),
            encoding="utf-8",
        )
        self.stop_path.chmod(0o644)
        document = scr.load_stop_file(self.stop_path)
        self.assertEqual(document["projects"], [self.project_a.local_path])
        self.assertEqual(document.get("agents", []), [])
        self.assertEqual(self.stop_path.stat().st_mode & 0o777, 0o600)

    def test_world_writable_legacy_stop_file_fails_closed(self):
        self.stop_path.write_text('{"projects": ["*"]}\n', encoding="utf-8")
        self.stop_path.chmod(0o666)
        with self.assertRaisesRegex(Exception, "writable by another user"):
            scr.load_stop_file(self.stop_path)
        self.assertEqual(self.stop_path.stat().st_mode & 0o777, 0o666)

    def test_global_stop_and_resume_are_rejected_from_slack(self):
        with patch.object(scr, "STOP_PATH", self.stop_path):
            self.assertIn("not supported", scr.apply_stop(self.project_a.local_path, "all"))
            self.assertFalse(self.stop_path.exists())
            scr.write_stop_file(["*"], "local", self.stop_path)
            message = scr.apply_resume(self.project_a.local_path, "project", self.stop_path)
            self.assertIn("local-operator-only", message)
            self.assertEqual(json.loads(self.stop_path.read_text())["projects"], ["*"])

    def test_agent_stop_does_not_stop_peer_project_or_agent(self):
        scr.apply_stop(self.project_a.local_path, "cursor-1", self.stop_path)
        document = scr.load_stop_file(self.stop_path)
        self.assertTrue(scr.agent_stop_applies(self.project_a.local_path, "cursor-1", document))
        self.assertFalse(scr.agent_stop_applies(self.project_a.local_path, "codex-1", document))
        self.assertFalse(scr.agent_stop_applies(self.project_b.local_path, "cursor-1", document))

    def test_status_filters_peer_project_stop_state(self):
        scr.write_stop_file(
            [self.project_b.local_path], "test", self.stop_path,
            agents=[f"{self.project_b.local_path}::cursor-1"],
        )
        with patch.object(scr, "STOP_PATH", self.stop_path), \
             patch.object(scr, "evaluate_fleet_status", return_value={
                 "state": "waiting", "summary": "waiting", "reasons": [],
             }), \
             patch.object(scr, "load_capacity", return_value={
                 "concurrent": [], "ready_total": 0,
             }), \
             patch.object(scr, "load_loop_heartbeats", return_value=[]):
            text = scr.status_text(self.project_a)
        self.assertNotIn(self.project_b.local_path, text)
        self.assertNotIn("operator stop:", text)

    def test_status_collection_is_serialized_across_project_channels(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def evaluate(_path):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with guard:
                active -= 1
            return {"state": "waiting", "summary": "waiting", "reasons": []}

        with patch.object(scr, "STOP_PATH", self.stop_path), \
             patch.object(scr, "evaluate_fleet_status", side_effect=evaluate), \
             patch.object(scr, "load_capacity", return_value={
                 "concurrent": [], "ready_total": 0,
             }), \
             patch.object(scr, "load_loop_heartbeats", return_value=[]), \
             ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(scr.status_text, self.project_a),
                executor.submit(scr.status_text, self.project_b),
            ]
            for future in futures:
                future.result()
        self.assertEqual(maximum, 1)

    def test_degraded_project_allows_status_but_blocks_mutation(self):
        self.checkout_a.rmdir()
        degraded = self.registry.get(self.project_a.project_id)
        with patch.object(scr, "evaluate_fleet_status") as fleet:
            status = scr.handle_command(sample_config(), {"verb": "status"}, degraded)
            fleet.assert_not_called()
        callback = []
        message = scr.handle_command(
            sample_config(),
            {"verb": "intervention", "ref": "187", "decision": "go", "kind": "issue"},
            degraded,
            lambda *args: callback.append(args) or True,
        )
        self.assertIn("degraded_unreachable", status)
        self.assertIn("degraded", message)
        self.assertEqual(callback, [])

    def test_reused_checkout_with_wrong_identity_blocks_github_mutation(self):
        self.registry.identity_provider = lambda path: {
            "github_repo_id": "R_wrong",
            "github_repo_database_id": 999,
            "project_v2_id": "P_wrong",
            "repo_slug": "owner/wrong",
            "local_path": str(path.resolve()),
        }
        comments = []
        replies = []
        reply = scr.handle_slack_message(
            sample_config(), self.registry,
            self.payload(text="intervention #187 do it", event_id="wrong-identity"),
            set(),
            comment=lambda *args: comments.append(args) or True,
            notify=lambda _config, event: replies.append(event["text"]) or {"ok": True},
            seen_path=self.seen_path,
        )
        self.assertIn("degraded", reply)
        self.assertEqual(comments, [])
        self.assertEqual(len(replies), 1)

    def test_legacy_raw_event_id_prevents_replayed_command(self):
        self.seen_path.write_text(
            json.dumps({"ids": {"legacy-event": "2026-08-14T00:00:00Z"}}),
            encoding="utf-8",
        )
        self.seen_path.chmod(0o600)
        calls = []
        with patch.object(scr, "STOP_PATH", self.stop_path):
            reply = scr.handle_slack_message(
                sample_config(), self.registry,
                self.payload(text="stop", event_id="legacy-event"), set(),
                comment=lambda *args: calls.append(args) or True,
                notify=lambda *args: calls.append(args) or {"ok": True},
                seen_path=self.seen_path,
            )
        self.assertIsNone(reply)
        self.assertEqual(calls, [])
        self.assertFalse(self.stop_path.exists())

    def test_intervention_targets_only_resolved_checkout(self):
        calls = []
        reply = scr.handle_command(
            sample_config(),
            {"verb": "intervention", "ref": "187", "decision": "approved", "kind": "issue"},
            self.project_b,
            lambda *args: calls.append(args) or True,
        )
        self.assertIn("GitHub issue #187", reply)
        self.assertEqual(calls, [("issue", 187, "approved", self.project_b.local_path)])

    def test_corrupt_seen_store_fails_closed_without_replacement(self):
        self.seen_path.write_text("{broken", encoding="utf-8")
        self.seen_path.chmod(0o600)
        before = self.seen_path.read_bytes()
        with self.assertRaises(Exception):
            scr.record_seen_id("key", self.seen_path)
        self.assertEqual(self.seen_path.read_bytes(), before)

    def test_start_refuses_missing_operator_before_opening_bridge(self):
        code = scr.start_bridge(sample_config(operator_user_id=""), self.registry)
        self.assertEqual(code, 1)

    def test_start_refuses_registry_from_another_workspace(self):
        code = scr.start_bridge(sample_config(team_id="T99999999"), self.registry)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
