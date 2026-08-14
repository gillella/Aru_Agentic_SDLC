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
    FileDedupeCache,
    RejectRedirectHandler,
    SlackConfig,
    config_for_project,
    config_from_env,
    dedupe_key,
    format_event,
    format_github_alert_comment,
    load_slack_env,
    notify_alert,
    post_event,
    redact,
    secrets_from_config,
    validate_alert_event,
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
        self.assertNotEqual(
            dedupe_key({**base, "project_id": "proj_a", "dedupe_key": "event-1"}),
            dedupe_key({**base, "project_id": "proj_b", "dedupe_key": "event-1"}),
        )

    def test_dedupe_key_hashes_sensitive_text(self):
        secret = "xoxb-" + ("a" * 40)
        key = dedupe_key(
            {
                "type": "hitl",
                "agent": "cursor-1",
                "project_id": "proj_a",
                "text": f"leak {secret}",
            }
        )
        self.assertNotIn(secret, key)
        self.assertIn("sha256:", key)

    def test_post_event_retries_after_slack_failure(self):
        calls = []

        def transport(config, text, thread_ts):
            calls.append(text)
            if len(calls) == 1:
                raise URLError("down")
            return {"ok": True, "ts": "2"}

        cache = DedupeCache()
        event = {"type": "blocked", "agent": "cursor-1", "issue": 1, "text": "x"}
        first = post_event(sample_config(), event, transport=transport, cache=cache)
        second = post_event(sample_config(), event, transport=transport, cache=cache)
        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"])
        self.assertFalse(second.get("deduped"))
        self.assertEqual(len(calls), 2)

    def test_notify_alert_dedupes_github_and_comments_pr(self):
        comments = []
        calls = []

        def transport(config, text, thread_ts):
            calls.append(text)
            return {"ok": True, "ts": "1"}

        def comment(kind, number, body, repo_dir):
            comments.append((kind, number))
            return True

        cache = DedupeCache()
        event = {
            "type": "hitl",
            "agent": "cursor-1",
            "family": "openai",
            "issue": 181,
            "pr": 201,
            "text": "merge close-out failed",
            "project_id": "proj_a",
        }
        first = notify_alert(
            sample_config(operator_user_id="U01234567"),
            event,
            transport=transport,
            cache=cache,
            comment=comment,
        )
        second = notify_alert(
            sample_config(operator_user_id="U01234567"),
            event,
            transport=transport,
            cache=cache,
            comment=comment,
        )
        self.assertTrue(first["ok"])
        self.assertTrue(second.get("deduped"))
        self.assertEqual(comments, [("pr", 201), ("issue", 181)])
        self.assertEqual(len(calls), 1)

    def test_notify_alert_skips_github_on_slack_retry(self):
        comments = []
        calls = []

        def transport(config, text, thread_ts):
            calls.append(text)
            if len(calls) == 1:
                raise URLError("down")
            return {"ok": True, "ts": "2"}

        def comment(kind, number, body, repo_dir):
            comments.append((kind, number))
            return True

        cache = DedupeCache()
        event = {
            "type": "blocked",
            "agent": "cursor-1",
            "issue": 9,
            "text": "depends-on",
            "project_id": "proj_a",
        }
        first = notify_alert(
            sample_config(), event, transport=transport, cache=cache, comment=comment
        )
        second = notify_alert(
            sample_config(), event, transport=transport, cache=cache, comment=comment
        )
        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(comments, [("issue", 9)])
        self.assertEqual(len(calls), 2)

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

    def test_forbidden_event_types_are_rejected(self):
        calls = []

        def transport(config, text, thread_ts):
            calls.append(text)
            return {"ok": True, "ts": "1"}

        result = post_event(
            sample_config(),
            {"type": "heartbeat", "agent": "cursor-1", "text": "tick"},
            transport=transport,
            cache=DedupeCache(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "forbidden_event_type")
        self.assertEqual(calls, [])

    def test_waiting_on_formats_peer_and_requires_fields(self):
        with self.assertRaises(ValueError):
            validate_alert_event({"type": "waiting-on", "agent": "cursor-1"})
        text = format_event(
            {
                "type": "waiting-on",
                "agent": "cursor-1",
                "family": "other",
                "issue": 181,
                "waiting_on_agent": "claude-1",
                "waiting_on_issue": 163,
                "text": "path conflict",
                "ts": "2026-08-14T12:00:00Z",
            }
        )
        self.assertIn("waiting on agent=`claude-1`", text)
        self.assertIn("issue #163", text)
        self.assertIn("claim not stolen", text)

    def test_hitl_mentions_operator(self):
        text = format_event(
            {
                "type": "hitl",
                "agent": "cursor-1",
                "operator_user_id": "U01234567",
                "text": "need schema decision",
                "ts": "2026-08-14T12:00:00Z",
            }
        )
        self.assertIn("<@U01234567>", text)
        self.assertIn("need schema decision", text)

    def test_notify_alert_comments_github_before_slack(self):
        order = []

        def transport(config, text, thread_ts):
            order.append(("slack", text))
            return {"ok": True, "ts": "9.9"}

        def comment(kind, number, body, repo_dir):
            order.append(("github", kind, number, body))
            return True

        secret = "arbitrary-signing-secret-value"
        result = notify_alert(
            sample_config(signing_secret=secret, operator_user_id="U01234567"),
            {
                "type": "hitl",
                "agent": "cursor-1",
                "family": "other",
                "issue": 181,
                "text": f"decision with {secret}",
                "project_id": "proj_test",
            },
            transport=transport,
            cache=DedupeCache(),
            comment=comment,
            repo_dir="/tmp/repo",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["github_ok"])
        self.assertEqual(order[0][0], "github")
        self.assertEqual(order[1][0], "slack")
        self.assertNotIn(secret, order[0][3])
        self.assertNotIn(secret, order[1][1])
        self.assertIn("<@U01234567>", order[1][1])

    def test_notify_alert_waiting_on_does_not_claim(self):
        claims = []

        def transport(config, text, thread_ts):
            return {"ok": True, "ts": "1"}

        def comment(kind, number, body, repo_dir):
            claims.append(("comment", kind, number))
            return True

        result = notify_alert(
            sample_config(),
            {
                "type": "waiting-on",
                "agent": "cursor-1",
                "family": "other",
                "issue": 181,
                "waiting_on_agent": "claude-1",
                "waiting_on_pr": 170,
                "text": "review in flight",
            },
            transport=transport,
            cache=DedupeCache(),
            comment=comment,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(claims, [("comment", "issue", 181)])
        self.assertIn("waiting on agent: `claude-1`", result["github_body"])
        self.assertIn("do not steal the claim", result["github_body"])

    def test_notify_alert_slack_down_still_returns(self):
        def transport(config, text, thread_ts):
            raise URLError("down")

        result = notify_alert(
            sample_config(),
            {
                "type": "blocked",
                "agent": "cursor-1",
                "issue": 1,
                "text": "depends-on #2",
            },
            transport=transport,
            cache=DedupeCache(),
            comment=lambda *a: True,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["slack"]["error"], "slack_unavailable")

    def test_file_dedupe_survives_reload(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "dedupe.json"
            first = FileDedupeCache(path)
            event = {
                "type": "blocked",
                "agent": "cursor-1",
                "issue": 1,
                "text": "x",
                "project_id": "proj_a",
            }
            calls = []

            def transport(config, text, thread_ts):
                calls.append(text)
                return {"ok": True, "ts": "1"}

            post_event(sample_config(), event, transport=transport, cache=first)
            second = FileDedupeCache(path)
            again = post_event(sample_config(), event, transport=transport, cache=second)
            self.assertTrue(again.get("deduped"))
            self.assertEqual(len(calls), 1)

    def test_github_alert_comment_has_no_slack_mention(self):
        body = format_github_alert_comment(
            {
                "type": "hitl",
                "agent": "cursor-1",
                "operator_user_id": "U01234567",
                "text": "need decision",
            }
        )
        self.assertNotIn("<@U01234567>", body)
        self.assertIn("HITL", body)


if __name__ == "__main__":
    unittest.main()
