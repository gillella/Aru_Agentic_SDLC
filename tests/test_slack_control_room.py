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
    def test_parse_commands(self):
        self.assertEqual(scr.parse_command("<@U123> status")["verb"], "status")
        stop = scr.parse_command("stop cursor-1")
        self.assertEqual(stop["verb"], "stop")
        self.assertEqual(stop["target"], "cursor-1")
        inter = scr.parse_command("intervention #172 ship it")
        self.assertEqual(inter["ref"], "172")
        self.assertEqual(inter["decision"], "ship it")
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

    def test_status_uses_fleet_status(self):
        fake = {
            "state": "waiting",
            "summary": "WAITING: 2 open issue(s)",
            "open_issues_count": 2,
            "open_prs_count": 1,
            "active_claims": ["cursor-1:#172"],
            "codebase_health": {"loc": 10, "file_count": 2},
        }
        with patch.object(scr, "evaluate_fleet_status", return_value=fake), \
             patch.object(scr, "load_stop_file", return_value={}):
            text = scr.status_text("/repo")
        self.assertIn("waiting", text)
        self.assertIn("cursor-1:#172", text)

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

    def test_manifest_subscribes_app_mention(self):
        text = (ROOT / "templates" / "slack" / "manifest.yaml").read_text(encoding="utf-8")
        self.assertIn("event_subscriptions:", text)
        self.assertIn("app_mention", text)

    def test_start_without_operator_fails_closed(self):
        with patch.object(scr, "_bolt_available", return_value=True):
            code = scr.start_bridge(sample_config(operator_user_id=""), "/repo", ".")
        self.assertEqual(code, 1)

    def test_start_without_bolt_fails_closed(self):
        with patch.object(scr, "_bolt_available", return_value=False):
            code = scr.start_bridge(sample_config(), "/repo", ".")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
