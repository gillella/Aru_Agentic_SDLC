import sys
import tempfile
import unittest
import os
import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from slack_notify import (  # noqa: E402
    DedupeCache,
    RejectRedirectHandler,
    SlackConfig,
    config_for_project,
    config_from_env,
    dedupe_key,
    format_event,
    load_slack_env,
    post_event,
    redact,
    secrets_from_config,
    main,
)
from slack_projects import ProjectRegistry  # noqa: E402


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

    def test_workspace_config_does_not_require_legacy_channel(self):
        config = config_from_env(
            {
                "SLACK_BOT_TOKEN": "xoxb-" + ("a" * 40),
                "SLACK_TEAM_ID": "T01234567",
            },
            require_channel=False,
        )
        self.assertEqual(config.channel_id, "")

    def test_project_record_is_the_only_outbound_destination(self):
        project = SimpleNamespace(
            slack_team_id="T01234567", slack_channel_id="C99999999"
        )
        routed = config_for_project(sample_config(channel_id="CLEGACY1"), project)
        self.assertEqual(routed.channel_id, "C99999999")
        with self.assertRaises(ValueError):
            config_for_project(
                sample_config(),
                SimpleNamespace(slack_team_id="T99999999", slack_channel_id="C99999999"),
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

    def test_dedupe_key_is_project_scoped(self):
        base = {"type": "state", "agent": "codex-1", "text": "same"}
        self.assertNotEqual(
            dedupe_key({**base, "project_id": "proj_a"}),
            dedupe_key({**base, "project_id": "proj_b"}),
        )

    def test_cli_requires_explicit_project_id(self):
        with self.assertRaises(SystemExit):
            main(["--agent", "codex-1", "--family", "openai", "--event", "state"])

    def test_cli_routes_by_registry_and_ignores_legacy_channel(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            checkout = root / "checkout"
            checkout.mkdir()
            registry_path = root / "projects.json"
            audit_path = root / "audit.json"

            def identity(path):
                return {
                    "github_repo_id": "R_repo",
                    "github_repo_database_id": 1,
                    "project_v2_id": "P_project",
                    "repo_slug": "owner/repo",
                    "local_path": str(path.resolve()),
                }

            record = ProjectRegistry(registry_path, audit_path, identity).create(
                checkout, "T01234567", "C99999999", "operator", "proj_outbound"
            )
            env_file = root / "slack.env"
            env_file.write_text(
                "SLACK_BOT_TOKEN=xoxb-" + ("a" * 40)
                + "\nSLACK_TEAM_ID=T01234567\nSLACK_CHANNEL_ID=C11111111\n",
                encoding="utf-8",
            )
            with patch("slack_notify.post_event", return_value={"ok": True}) as posted:
                code = main([
                    "--agent", "codex-1", "--family", "openai", "--event", "state",
                    "--project-id", record.project_id,
                    "--registry-file", str(registry_path), "--env-file", str(env_file),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(posted.call_args.args[0].channel_id, "C99999999")

    def test_unknown_outbound_project_fails_closed_without_posting(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            registry_path = root / "projects.json"
            env_file = root / "slack.env"
            env_file.write_text(
                "SLACK_BOT_TOKEN=xoxb-" + ("a" * 40)
                + "\nSLACK_TEAM_ID=T01234567\nSLACK_CHANNEL_ID=C11111111\n",
                encoding="utf-8",
            )
            with patch("slack_notify.post_event") as posted:
                code = main([
                    "--agent", "codex-1", "--family", "openai", "--event", "state",
                    "--project-id", "proj_missing", "--registry-file", str(registry_path),
                    "--env-file", str(env_file),
                ])
            self.assertEqual(code, 0)
            posted.assert_not_called()

    def test_cli_skips_cleanly_when_registry_module_cannot_import(self):
        original_import = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == "slack_projects":
                raise ImportError("registry module unavailable")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=unavailable):
            code = main([
                "--agent", "codex-1", "--family", "openai", "--event", "state",
                "--project-id", "proj_missing",
            ])
        self.assertEqual(code, 0)

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
