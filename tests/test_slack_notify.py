import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from slack_notify import (  # noqa: E402
    DedupeCache,
    RejectRedirectHandler,
    SlackConfig,
    config_from_env,
    format_event,
    load_slack_env,
    post_event,
    redact,
    secrets_from_config,
)


def sample_config(**kwargs):
    data = {
        "bot_token": "xoxb-" + ("a" * 40),
        "team_id": "T01234567",
        "channel_id": "C01234567",
    }
    data.update(kwargs)
    return SlackConfig(**data)


class SlackNotifyTests(unittest.TestCase):
    def test_redact_strips_configured_secrets(self):
        secret = "arbitrary-signing-secret-value"
        config = sample_config(signing_secret=secret)
        text = format_event({"type": "hitl", "text": f"leak {secret}"}, secrets=secrets_from_config(config))
        self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)

    def test_redact_strips_bot_tokens(self):
        # Shape must not match Slack's live token grammar or push protection.
        text = "leak xoxb-PLACEHOLDERTOKENVALUE and Bearer secret"
        self.assertNotIn("xoxb-PLACEHOLDERTOKENVALUE", redact(text))
        self.assertNotIn("Bearer secret", redact(text))
        self.assertIn("[redacted]", redact(text))

    def test_load_env_prefers_process_environment(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "slack.env"
            path.write_text("SLACK_TEAM_ID=TFILE0001\n", encoding="utf-8")
            with patch.dict("os.environ", {"SLACK_TEAM_ID": "TENV00001"}, clear=False):
                values = load_slack_env(path)
        self.assertEqual(values["SLACK_TEAM_ID"], "TENV00001")

    def test_config_rejects_placeholder_channel(self):
        with self.assertRaises(ValueError):
            config_from_env(
                {
                    "SLACK_BOT_TOKEN": "xoxb-" + ("a" * 40),
                    "SLACK_TEAM_ID": "T01234567",
                    "SLACK_CHANNEL_ID": "C",
                }
            )

    def test_format_event_stamps_identity(self):
        text = format_event(
            {
                "type": "blocked",
                "agent": "cursor-1",
                "family": "xai",
                "repo": "gillella/Aru_Agentic_SDLC",
                "issue": 172,
                "state": "In Progress",
                "text": "waiting on #110",
                "ts": "2026-08-14T12:00:00Z",
            }
        )
        self.assertIn("agent=`cursor-1`", text)
        self.assertIn("family=`xai`", text)
        self.assertIn("issue #172", text)
        self.assertIn("waiting on #110", text)

    def test_post_event_dedupes(self):
        calls = []

        def transport(config, text, thread_ts):
            calls.append(text)
            return {"ok": True, "ts": "1.2"}

        cache = DedupeCache()
        event = {"type": "blocked", "agent": "cursor-1", "issue": 1, "text": "x"}
        first = post_event(sample_config(), event, transport=transport, cache=cache)
        second = post_event(sample_config(), event, transport=transport, cache=cache)
        self.assertTrue(first["ok"])
        self.assertTrue(second.get("deduped"))
        self.assertEqual(len(calls), 1)

    def test_post_event_slack_down_does_not_raise(self):
        def transport(config, text, thread_ts):
            raise URLError("down")

        result = post_event(
            sample_config(),
            {"type": "hitl", "agent": "cursor-1", "text": "need decision"},
            transport=transport,
            cache=DedupeCache(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "slack_unavailable")

    def test_transport_rejects_redirects(self):
        from urllib.request import Request

        handler = RejectRedirectHandler()
        req = Request("https://slack.com/api/chat.postMessage")
        with self.assertRaises(URLError) as ctx:
            handler.redirect_request(
                req, None, 302, "Found", {}, "https://evil.example/steal"
            )
        self.assertIn("slack_redirect_rejected", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
