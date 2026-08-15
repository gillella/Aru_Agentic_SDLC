import sys
import tempfile
import unittest
import os
import builtins
import json
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

    def test_notify_alert_retries_failed_github_before_slack(self):
        comments = []
        slack = []

        def comment(kind, number, body, repo_dir):
            comments.append((kind, number))
            return len(comments) > 1

        def transport(config, text, thread_ts):
            slack.append(text)
            return {"ok": True, "ts": "1"}

        event = {
            "type": "blocked",
            "agent": "cursor-1",
            "issue": 181,
            "text": "dependency unavailable",
            "project_id": "proj_a",
        }
        cache = DedupeCache()
        first = notify_alert(
            sample_config(), event, transport=transport, cache=cache, comment=comment
        )
        second = notify_alert(
            sample_config(), event, transport=transport, cache=cache, comment=comment
        )
        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(comments, [("issue", 181), ("issue", 181)])
        self.assertEqual(len(slack), 1)

    def test_notify_alert_retries_only_failed_github_target(self):
        comments = []
        slack = []
        issue_attempts = 0

        def comment(kind, number, body, repo_dir):
            nonlocal issue_attempts
            comments.append((kind, number))
            if kind == "issue":
                issue_attempts += 1
                return issue_attempts > 1
            return True

        event = {
            "type": "blocked",
            "agent": "cursor-1",
            "issue": 181,
            "pr": 201,
            "text": "dependency unavailable",
            "project_id": "proj_a",
        }
        cache = DedupeCache()
        first = notify_alert(
            sample_config(),
            event,
            transport=lambda *_a: slack.append("posted") or {"ok": True},
            cache=cache,
            comment=comment,
        )
        second = notify_alert(
            sample_config(),
            event,
            transport=lambda *_a: slack.append("posted") or {"ok": True},
            cache=cache,
            comment=comment,
        )
        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(
            comments, [("pr", 201), ("issue", 181), ("issue", 181)]
        )
        self.assertEqual(slack, ["posted"])

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
            sample_config(operator_user_id="U01234567"),
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

    def test_allowed_type_rejects_forbidden_payload_content(self):
        calls = []
        for body in (
            "raw diff: +secret",
            "prompt: do this",
            "test log: failed",
            "tokens used: 500",
        ):
            result = notify_alert(
                sample_config(),
                {
                    "type": "blocked",
                    "agent": "cursor-1",
                    "issue": 181,
                    "text": body,
                },
                transport=lambda *_a: calls.append("slack") or {"ok": True},
                cache=DedupeCache(),
                comment=lambda *_a: calls.append("github") or True,
            )
            self.assertEqual(result["error"], "invalid_alert")
        self.assertEqual(calls, [])

    def test_all_formatted_fields_redact_common_credentials(self):
        secrets = [
            "github_pat_" + ("A" * 24),
            "ghp_" + ("B" * 24),
            "AKIA" + ("C" * 16),
            "password=" + ("D" * 20),
            "AWS_SECRET_ACCESS_KEY=" + ("E" * 40),
        ]
        posted = []
        comments = []
        result = notify_alert(
            sample_config(),
            {
                "type": "blocked",
                "agent": secrets[0],
                "family": secrets[1],
                "repo": secrets[2],
                "state": secrets[3],
                "issue": 181,
                "text": "safe blocker summary " + secrets[4],
                "project_id": "proj_a",
            },
            transport=lambda _c, text, _t: posted.append(text) or {"ok": True},
            cache=DedupeCache(),
            comment=lambda _k, _n, body, _r: comments.append(body) or True,
        )
        self.assertTrue(result["ok"])
        combined = "\n".join([*posted, *comments])
        for secret in secrets:
            self.assertNotIn(secret, combined)
        self.assertIn("[redacted]", combined)

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

    def test_validate_alert_event_normalizes_type_in_place(self):
        event = {
            "type": " HITL ",
            "agent": "cursor-1",
            "text": "need a decision",
        }
        validate_alert_event(event)
        self.assertEqual(event["type"], "hitl")

    def test_notify_alert_uppercase_hitl_injects_operator(self):
        config = sample_config(operator_user_id="U01234567")
        posted = []

        def transport(cfg, text, thread_ts=None):
            posted.append(text)
            return {"ok": True, "ts": "1"}

        result = notify_alert(
            config,
            {
                "type": "HITL",
                "agent": "cursor-1",
                "issue": 9,
                "text": "need a decision",
                "project_id": "proj_a",
            },
            transport=transport,
            cache=DedupeCache(),
            comment=lambda *_a, **_k: True,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(posted)
        self.assertIn("<@U01234567>", posted[0])

    def test_hitl_uses_only_configured_operator_identity(self):
        posted = []
        event = {
            "type": "hitl",
            "agent": "cursor-1",
            "issue": 181,
            "text": "need a decision",
            "operator_user_id": "U99999999",
        }
        result = notify_alert(
            sample_config(operator_user_id="U01234567"),
            event,
            transport=lambda _c, text, _t: posted.append(text) or {"ok": True},
            cache=DedupeCache(),
            comment=lambda *_a: True,
        )
        self.assertTrue(result["ok"])
        self.assertIn("<@U01234567>", posted[0])
        self.assertNotIn("U99999999", posted[0])

        direct = []
        direct_result = post_event(
            sample_config(operator_user_id="U01234567"),
            event,
            transport=lambda _c, text, _t: direct.append(text) or {"ok": True},
            cache=DedupeCache(),
        )
        self.assertTrue(direct_result["ok"])
        self.assertIn("<@U01234567>", direct[0])
        self.assertNotIn("U99999999", direct[0])

    def test_hitl_rejects_missing_or_malformed_configured_operator(self):
        for operator in ("", "U1", "C01234567", "U0123-456"):
            event = {
                "type": "hitl",
                "agent": "cursor-1",
                "issue": 181,
                "text": "need a decision",
            }
            result = notify_alert(
                sample_config(operator_user_id=operator),
                event,
                cache=DedupeCache(),
                comment=lambda *_a: True,
            )
            self.assertEqual(result["error"], "invalid_alert")
            direct = post_event(
                sample_config(operator_user_id=operator),
                event,
                cache=DedupeCache(),
            )
            self.assertEqual(direct["error"], "invalid_operator_user_id")

    def test_file_dedupe_importerror_fallback_merges_existing(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "dedupe.json"
            path.write_text(
                '{"entries": {"keep-me": 9999999999.0}}\n',
                encoding="utf-8",
            )
            cache = FileDedupeCache(path)
            cache._seen = {"new-key": 9999999999.0}
            real_import = builtins.__import__

            def fake_import(name, *args, **kwargs):
                if name == "slack_projects":
                    raise ImportError("forced")
                return real_import(name, *args, **kwargs)

            with patch.object(builtins, "__import__", side_effect=fake_import):
                cache._persist()
            payload = __import__("json").loads(path.read_text(encoding="utf-8"))
            self.assertIn("keep-me", payload["entries"])
            self.assertIn("new-key", payload["entries"])

    def test_notify_alert_marks_missing_github_target(self):
        result = notify_alert(
            sample_config(),
            {
                "type": "blocked",
                "agent": "cursor-1",
                "text": "stuck",
                "project_id": "proj_a",
            },
            transport=lambda *_a, **_k: {"ok": True, "ts": "1"},
            cache=DedupeCache(),
        )
        self.assertFalse(result["ok"])
        self.assertIs(result["github_ok"], False)

    def test_notify_alert_rejects_invalid_programmatic_targets_without_raising(self):
        calls = []
        for issue in ("not-a-number", [], True, -1):
            result = notify_alert(
                sample_config(),
                {
                    "type": "blocked",
                    "agent": "cursor-1",
                    "issue": issue,
                    "text": "dependency unavailable",
                },
                transport=lambda *_a: calls.append("slack") or {"ok": True},
                comment=lambda *_a: calls.append("github") or True,
            )
            self.assertEqual(result["error"], "invalid_alert")
        self.assertEqual(calls, [])

    def test_notify_alert_refuses_slack_only_bypass(self):
        calls = []
        result = notify_alert(
            sample_config(),
            {
                "type": "blocked",
                "agent": "cursor-1",
                "issue": 181,
                "text": "dependency unavailable",
            },
            skip_github=True,
            transport=lambda *_a: calls.append("slack") or {"ok": True},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_alert")
        self.assertEqual(calls, [])

    def test_slack_failure_audit_persists_and_records_recovery(self):
        with tempfile.TemporaryDirectory() as raw:
            audit_path = Path(raw) / "notify-audit.json"
            calls = []

            def transport(config, text, thread_ts):
                calls.append(text)
                if len(calls) == 1:
                    raise URLError("down")
                return {"ok": True, "ts": "2"}

            event = {
                "type": "blocked",
                "agent": "ghp_" + ("Z" * 24),
                "issue": 181,
                "text": "dependency unavailable",
                "project_id": "proj_a",
            }
            cache = DedupeCache()
            first = notify_alert(
                sample_config(),
                event,
                transport=transport,
                cache=cache,
                comment=lambda *_a: True,
                audit_path=audit_path,
            )
            second = notify_alert(
                sample_config(),
                event,
                transport=transport,
                cache=cache,
                comment=lambda *_a: True,
                audit_path=audit_path,
            )
            self.assertFalse(first["ok"])
            self.assertTrue(second["ok"])
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["retry_state"] for item in payload["events"]],
                ["pending_retry", "complete"],
            )
            self.assertEqual(
                [item["outcome"] for item in payload["events"]],
                ["slack_failed", "delivered"],
            )
            self.assertTrue(all("sha256:" in item["dedupe_id"] for item in payload["events"]))
            self.assertNotIn("ghp_", audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit_path.stat().st_mode & 0o777, 0o600)

    def test_ci_remediation_skill_contains_blocked_hitl_alert_contract(self):
        skill = (ROOT / "skills" / "remediate-ci-failure" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("scripts/slack_notify.py", skill)
        self.assertIn("--event blocked", skill)
        self.assertIn("--event hitl", skill)
        self.assertIn("structured failure audit", skill)

    def test_skill_alert_examples_name_every_required_cli_flag(self):
        for relative in (
            "skills/implement-next-issue/SKILL.md",
            "skills/run-aru-factory/SKILL.md",
        ):
            skill = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("--project-id <PROJECT_ID>", skill)
            self.assertIn("--agent <AGENT_ID>", skill)
            self.assertIn("--family <FAMILY>", skill)
            self.assertIn("--event", skill)

    def test_main_warns_when_github_comment_fails(self):
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
                checkout, "T01234567", "C01234567", "operator", "proj_outbound"
            )
            env_file = root / "slack.env"
            env_file.write_text(
                "SLACK_BOT_TOKEN=xoxb-" + ("a" * 40)
                + "\nSLACK_TEAM_ID=T01234567\n",
                encoding="utf-8",
            )
            with patch(
                "slack_notify.notify_alert",
                return_value={
                    "ok": True,
                    "slack": {"ok": True},
                    "github_ok": False,
                    "deduped": False,
                },
            ):
                code = main([
                    "--agent", "cursor-1", "--family", "openai", "--event", "blocked",
                    "--project-id", record.project_id, "--issue", "1",
                    "--registry-file", str(registry_path), "--env-file", str(env_file),
                ])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
