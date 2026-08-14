import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import slack_control_room as scr  # noqa: E402
from slack_notify import SlackConfig  # noqa: E402


def _load_manifest(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        parsed = yaml.safe_load(text)
        if isinstance(parsed, dict):
            return parsed
    except ImportError:
        pass
    events, scopes, section = [], [], None
    for line in text.splitlines():
        stripped = line.strip()
        if line.startswith("oauth_config:"):
            section = "oauth"
        elif line.startswith("settings:"):
            section = "settings"
        elif stripped == "bot:" and section == "oauth":
            section = "oauth_bot"
        elif stripped == "bot_events:":
            section = "bot_events"
        elif stripped.startswith("- ") and section == "bot_events":
            events.append(stripped[2:].strip())
        elif stripped.startswith("- ") and section == "oauth_bot":
            scopes.append(stripped[2:].strip())
        elif line and not line.startswith((" ", "\t")) and section in {"bot_events", "oauth_bot"}:
            section = None
    return {
        "settings": {"event_subscriptions": {"bot_events": events}},
        "oauth_config": {"scopes": {"bot": scopes}},
    }


def sample_config(**kwargs):
    data = {
        "bot_token": "xoxb-" + ("a" * 40),
        "team_id": "T01234567",
        "channel_id": "C01234567",
        "operator_user_id": "U01234567",
    }
    data.update(kwargs)
    return SlackConfig(**data)


class SlackControlRoomTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.seen_patch = patch.object(scr, "SEEN_PATH", tmp / "seen.json")
        self.pid_patch = patch.object(scr, "PID_PATH", tmp / "bridge.pid")
        self.stop_patch = patch.object(scr, "STOP_PATH", tmp / "factory-loop.stop")
        self.seen_patch.start()
        self.pid_patch.start()
        self.stop_patch.start()
        self.cap_patch = patch.object(
            scr, "load_capacity",
            return_value={"concurrent": [], "deferred": [], "ready_total": 0},
        )
        self.beat_patch = patch.object(
            scr, "load_loop_heartbeats",
            return_value=["cursor: last_heartbeat=unknown wake_evidence=none"],
        )
        self.cap_patch.start()
        self.beat_patch.start()
        self.addCleanup(self.seen_patch.stop)
        self.addCleanup(self.pid_patch.stop)
        self.addCleanup(self.stop_patch.stop)
        self.addCleanup(self.cap_patch.stop)
        self.addCleanup(self.beat_patch.stop)

    def test_parse_commands(self):
        self.assertEqual(scr.parse_command("<@U123> status")["verb"], "status")
        stop = scr.parse_command("stop cursor-1")
        self.assertEqual(stop["verb"], "stop")
        self.assertEqual(stop["target"], "cursor-1")
        inter = scr.parse_command("intervention #172 ship it")
        self.assertEqual(inter["ref"], "172")
        self.assertEqual(inter["kind"], "issue")
        self.assertEqual(inter["decision"], "ship it")
        pr_cmd = scr.parse_command("intervention pr #88 ship it")
        self.assertEqual(pr_cmd["kind"], "pr")
        self.assertEqual(pr_cmd["ref"], "88")
        self.assertIsNone(scr.parse_command("hello there"))
        self.assertIsNone(scr.parse_command("<@U123> please do not stop all"))

    def test_authorize_fail_closed(self):
        config = sample_config()
        self.assertTrue(scr.authorize(config, "T01234567", "C01234567", "U01234567"))
        self.assertFalse(scr.authorize(config, "TOTHER", "C01234567", "U01234567"))
        self.assertFalse(scr.authorize(config, "T01234567", "COTHER00", "U01234567"))
        self.assertFalse(scr.authorize(config, "T01234567", "C01234567", "UOTHER"))
        self.assertFalse(
            scr.authorize(sample_config(operator_user_id=""), "T01234567", "C01234567", "U01234567")
        )

    def test_unauthorized_message_is_ignored(self):
        seen = set()
        reply = scr.handle_slack_message(
            sample_config(),
            {
                "team": "T01234567",
                "channel": "C01234567",
                "user": "U999",
                "text": "status",
                "ts": "1.0",
            },
            seen,
            "/repo",
            ".",
            comment=lambda *_: True,
            notify=lambda *_args, **_kw: {"ok": True},
        )
        self.assertIsNone(reply)

    def test_duplicate_event_is_ignored(self):
        seen = {"dup"}
        reply = scr.handle_slack_message(
            sample_config(),
            {
                "team": "T01234567",
                "channel": "C01234567",
                "user": "U01234567",
                "text": "status",
                "client_msg_id": "dup",
            },
            seen,
            "/repo",
            ".",
            comment=lambda *_: True,
            notify=lambda *_args, **_kw: {"ok": True},
        )
        self.assertIsNone(reply)

    def test_stop_and_resume_write_durable_file(self):
        with tempfile.TemporaryDirectory() as raw:
            stop = Path(raw) / "factory-loop.stop"
            with patch.object(scr, "STOP_PATH", stop):
                msg = scr.apply_stop("/abs/repo", "all")
                self.assertTrue(stop.is_file())
                data = json.loads(stop.read_text(encoding="utf-8"))
                self.assertIn("*", data["projects"])
                self.assertEqual(data["source"], "slack_control_room")
                self.assertIn("drain-first", msg)
                self.assertTrue(scr.agent_stop_applies("/abs/repo", "cursor-1", data))
                self.assertTrue(scr.agent_stop_applies("/abs/repo", "claude-1", data))
                msg = scr.apply_resume("/abs/repo", "all")
                self.assertFalse(stop.exists())
                self.assertIn("cleared", msg)

    def test_intervention_copies_to_github_callback(self):
        posted = []

        def comment(kind, number, decision, repo_dir):
            posted.append((kind, number, decision, repo_dir))
            return True

        parsed = scr.parse_command("intervention #88 do this")
        reply = scr.handle_command(
            sample_config(), parsed, "/repo", "/abs/checkout", comment
        )
        self.assertEqual(posted, [("issue", 88, "do this", "/abs/checkout")])
        self.assertIn("copied intervention", reply)

        pr_posted = []

        def comment_pr(kind, number, decision, repo_dir):
            pr_posted.append((kind, number, decision, repo_dir))
            return True

        parsed_pr = scr.parse_command("intervention pr #99 do this")
        secret = "arbitrary-signing-secret-value"
        cfg = sample_config(signing_secret=secret)
        parsed_secret = scr.parse_command(f"intervention #7 leak {secret} now")
        leaked = []
        scr.handle_command(
            cfg,
            parsed_secret,
            "/repo",
            "/abs/checkout",
            lambda kind, number, decision, repo_dir: leaked.append(decision) or True,
        )
        self.assertEqual(leaked, ["leak [redacted] now"])
        scr.handle_command(sample_config(), parsed_pr, "/repo", "/abs/checkout", comment_pr)
        self.assertEqual(pr_posted[0][0], "pr")
        self.assertEqual(pr_posted[0][1], 99)

    def test_status_uses_fleet_status(self):
        fake = {
            "state": "waiting",
            "summary": "WAITING: 2 open issue(s)",
            "open_issues_count": 2,
            "open_prs_count": 1,
            "active_claims": ["cursor-1:#172"],
            "codebase_health": {"loc": 10, "file_count": 2},
            "reasons": ["Issue #172 is In Review.", "PR #184 is open and pending review."],
        }
        cap = {"concurrent": [103], "deferred": [], "ready_total": 2}
        beats = ["cursor: last_heartbeat=unknown wake_evidence=none"]
        with patch.object(scr, "evaluate_fleet_status", return_value=fake), \
             patch.object(scr, "load_stop_file", return_value={}), \
             patch.object(scr, "load_capacity", return_value=cap), \
             patch.object(scr, "load_loop_heartbeats", return_value=beats):
            text = scr.status_text("/repo", "/repo")
        self.assertIn("waiting", text)
        self.assertIn("cursor-1:#172", text)
        self.assertIn("capacity:", text)
        self.assertIn("claimable=1", text)
        self.assertIn("open review work:", text)
        self.assertIn("Issue #172 is In Review.", text)
        self.assertIn("last_heartbeat=", text)

    def test_doctor_without_tokens(self):
        with tempfile.TemporaryDirectory() as raw:
            env = Path(raw) / "slack.env"
            report = scr.doctor(env)
        self.assertFalse(report["ok"])
        self.assertFalse(report["env_file_present"])

    def test_scoped_resume_preserves_global_stop(self):
        with tempfile.TemporaryDirectory() as raw:
            stop = Path(raw) / "factory-loop.stop"
            with patch.object(scr, "STOP_PATH", stop):
                scr.apply_stop("/abs/repo", "all")
                msg = scr.apply_resume("/abs/repo", "cursor-1")
                data = json.loads(stop.read_text(encoding="utf-8"))
                self.assertIn("*", data["projects"])
                self.assertIn("global stop", msg)

    def test_agent_stop_leaves_peer_running(self):
        with tempfile.TemporaryDirectory() as raw:
            stop = Path(raw) / "factory-loop.stop"
            with patch.object(scr, "STOP_PATH", stop):
                msg = scr.apply_stop("/abs/repo", "cursor-1")
                data = json.loads(stop.read_text(encoding="utf-8"))
                self.assertNotIn("/abs/repo", data["projects"])
                self.assertNotIn("*", data["projects"])
                self.assertIn("/abs/repo::cursor-1", data["agents"])
                self.assertIn("cursor-1", msg)
                self.assertTrue(scr.agent_stop_applies("/abs/repo", "cursor-1", data))
                self.assertFalse(scr.agent_stop_applies("/abs/repo", "claude-1", data))
                resume = scr.apply_resume("/abs/repo", "cursor-1")
                self.assertFalse(stop.exists())
                self.assertIn("cleared", resume)

    def test_manifest_subscribes_app_mention(self):
        parsed = _load_manifest(ROOT / "templates" / "slack" / "manifest.yaml")
        events = ((parsed.get("settings") or {}).get("event_subscriptions") or {}).get("bot_events") or []
        scopes = ((parsed.get("oauth_config") or {}).get("scopes") or {}).get("bot") or []
        self.assertIn("app_mention", events)
        self.assertIn("app_mentions:read", scopes)
        self.assertIn("chat:write", scopes)
        self.assertNotIn("channels:manage", scopes)
        self.assertNotIn("bookmarks:write", scopes)

    def test_start_without_operator_fails_closed(self):
        with patch.object(scr, "_bolt_available", return_value=True):
            code = scr.start_bridge(sample_config(operator_user_id=""), "/repo", ".")
        self.assertEqual(code, 1)

    def test_start_refuses_live_duplicate_pid(self):
        with patch.object(scr, "_bolt_available", return_value=True), \
             patch.object(scr, "_bridge_running", return_value=True):
            code = scr.start_bridge(
                sample_config(app_token="xapp-" + ("b" * 20)), "/repo", "."
            )
        self.assertEqual(code, 1)

    def test_start_without_bolt_fails_closed(self):
        with patch.object(scr, "_bolt_available", return_value=False):
            code = scr.start_bridge(sample_config(), "/repo", ".")
        self.assertEqual(code, 1)

    def test_duplicate_event_survives_new_bridge_instance(self):
        payload = {
            "team": "T01234567",
            "channel": "C01234567",
            "user": "U01234567",
            "text": "stop cursor-1",
            "client_msg_id": "replay-1",
        }
        first = scr.handle_slack_message(
            sample_config(),
            payload,
            set(),
            "/abs/repo",
            ".",
            comment=lambda *_: True,
            notify=lambda *_args, **_kw: {"ok": True},
        )
        with patch.object(scr, "apply_stop") as stop:
            second = scr.handle_slack_message(
                sample_config(),
                payload,
                set(),
                "/abs/repo",
                ".",
                comment=lambda *_: True,
                notify=lambda *_args, **_kw: {"ok": True},
            )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        stop.assert_not_called()

    def test_stop_bridge_refuses_live_foreign_pid(self):
        scr.PID_PATH.write_text(
            json.dumps({"pid": 4242, "identity": scr.BRIDGE_IDENTITY}),
            encoding="utf-8",
        )
        with patch.object(scr, "_pid_exists", return_value=True), \
             patch.object(scr, "_is_our_bridge", return_value=False), \
             patch.object(scr.os, "kill") as kill:
            msg = scr.stop_bridge()
        self.assertIn("refusing to signal pid 4242", msg)
        kill.assert_not_called()
        self.assertTrue(scr.PID_PATH.is_file())


if __name__ == "__main__":
    unittest.main()
