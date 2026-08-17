import hashlib
import json
import os
import subprocess
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
from delivery_increments import DeliveryIncrementStore  # noqa: E402
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
    def payload(
        channel="C01234567", event_id="evt-1", text="status", user="U01234567",
        ts="1786795200.000001",
    ):
        return {
            "team": "T01234567",
            "channel": channel,
            "user": user,
            "client_msg_id": event_id,
            "ts": ts,
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

    def test_parse_sprint_commands_are_explicit_and_deterministic(self):
        sha = "a" * 40
        authorize = scr.parse_command(
            f"sprint authorize control #300 issues #12,#10 baseline {sha}"
        )
        self.assertEqual(authorize["action"], "authorize")
        self.assertEqual(authorize["control_issue"], 300)
        self.assertEqual(authorize["issue_scope"], [10, 12])
        self.assertEqual(authorize["kind"], "normal")
        emergency = scr.parse_command(
            f"sprint authorize control 301 issues 99 baseline {sha} emergency"
        )
        self.assertEqual(emergency["kind"], "emergency")
        self.assertEqual(
            scr.parse_command("sprint accept inc_0123456789abcdef0123 risk-accepted")[
                "risk_accepted"
            ],
            True,
        )
        self.assertEqual(scr.parse_command("sprint do something")["verb"], "sprint-invalid")
        duplicate = scr.parse_command(
            f"sprint authorize control #300 issues #10,#10 baseline {sha}"
        )
        self.assertEqual(duplicate["verb"], "sprint-invalid")
        self.assertIn("duplicates", duplicate["decision"])
        for action in ("start", "authorize-deployment", "deployed", "cancel"):
            malformed = scr.parse_command(
                f"sprint {action} inc_0123456789abcdef0123 risk-accepted"
            )
            self.assertEqual(malformed["verb"], "sprint-invalid")

    def test_sprint_decision_records_github_before_state_and_dedupes(self):
        store = DeliveryIncrementStore(self.root / "increments.json")
        sha = "b" * 40
        recorded = []
        replies = []

        def recorder(control, payload, repo_dir):
            self.assertEqual(store.list(), [])
            recorded.append((control, payload, repo_dir))
            return "https://github.com/owner/checkout-a/issues/300#issuecomment-1"

        payload = self.payload(
            event_id="sprint-authorize-1",
            text=f"sprint authorize control #300 issues #12,#10 baseline {sha}",
        )
        reply = scr.handle_slack_message(
            sample_config(), self.registry, payload, set(),
            notify=lambda _config, event: replies.append(event["text"]) or {"ok": True},
            seen_path=self.seen_path,
            increment_store=store,
            baseline_verifier=lambda *_args: True,
            record_increment=recorder,
        )
        self.assertIn("sprint authorize recorded", reply)
        self.assertEqual(replies, [reply])
        self.assertEqual(len(recorded), 1)
        increment = store.list(self.project_a.project_id)[0]
        self.assertEqual(increment["issue_scope"], [10, 12])

        duplicate = scr.handle_slack_message(
            sample_config(), self.registry, payload, set(),
            notify=lambda *_args: self.fail("duplicate must not notify"),
            seen_path=self.seen_path,
            increment_store=store,
            baseline_verifier=lambda *_args: True,
            record_increment=recorder,
        )
        self.assertIsNone(duplicate)
        self.assertEqual(len(recorded), 1)

    def test_github_record_failure_pauses_without_consuming_retry(self):
        store = DeliveryIncrementStore(self.root / "increments.json")
        sha = "c" * 40
        payload = self.payload(
            event_id="retryable-sprint",
            text=f"sprint authorize control #300 issues #10 baseline {sha}",
        )
        failed = scr.handle_slack_message(
            sample_config(), self.registry, payload, set(),
            notify=lambda *_args: {"ok": True},
            seen_path=self.seen_path,
            increment_store=store,
            baseline_verifier=lambda *_args: True,
            record_increment=lambda *_args: None,
        )
        self.assertIn("GitHub decision record failed", failed)
        self.assertEqual(store.list(), [])
        self.assertFalse(self.seen_path.exists())

        retried = scr.handle_slack_message(
            sample_config(), self.registry, payload, set(),
            notify=lambda *_args: {"ok": True},
            seen_path=self.seen_path,
            increment_store=store,
            baseline_verifier=lambda *_args: True,
            record_increment=lambda *_args: (
                "https://github.com/owner/checkout-a/issues/300#issuecomment-2"
            ),
        )
        self.assertIn("sprint authorize recorded", retried)
        self.assertEqual(len(store.list()), 1)

    def test_invalid_baseline_pauses_before_github_or_state(self):
        store = DeliveryIncrementStore(self.root / "increments.json")
        calls = []
        reply = scr.handle_slack_message(
            sample_config(), self.registry,
            self.payload(
                event_id="bad-baseline",
                text=(
                    "sprint authorize control #300 issues #10 baseline " + "f" * 40
                ),
            ),
            set(), notify=lambda *_args: {"ok": True},
            seen_path=self.seen_path,
            increment_store=store,
            baseline_verifier=lambda *_args: False,
            record_increment=lambda *args: calls.append(args),
        )
        self.assertIn("baseline commit", reply)
        self.assertEqual(calls, [])
        self.assertEqual(store.list(), [])
        self.assertFalse(self.seen_path.exists())

    def test_corrupt_increment_encoding_pauses_before_github_or_state(self):
        store = DeliveryIncrementStore(self.root / "increments.json")
        store.path.write_bytes(b'{"schema": 1, "increments": []}\xff')
        store.path.chmod(0o600)
        calls = []
        original = store.path.read_bytes()
        reply = scr.handle_slack_message(
            sample_config(), self.registry,
            self.payload(
                event_id="corrupt-registry",
                text=(
                    "sprint authorize control #300 issues #10 baseline " + "a" * 40
                ),
            ),
            set(), notify=lambda *_args: {"ok": True},
            seen_path=self.seen_path,
            increment_store=store,
            baseline_verifier=lambda *_args: True,
            record_increment=lambda *args: calls.append(args),
        )
        self.assertIn("sprint decision paused", reply)
        self.assertIn("cannot read", reply)
        self.assertEqual(calls, [])
        self.assertEqual(store.path.read_bytes(), original)
        self.assertFalse(self.seen_path.exists())

    def test_baseline_verifier_requires_the_supplied_object_to_be_a_commit(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.checkout_a, check=True)
        subprocess.run(
            ["git", "config", "user.email", "tests@example.com"],
            cwd=self.checkout_a, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Aru Tests"],
            cwd=self.checkout_a, check=True,
        )
        (self.checkout_a / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.checkout_a, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=self.checkout_a, check=True)
        commit_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.checkout_a,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "tag", "-a", "baseline-tag", "-m", "annotated"],
            cwd=self.checkout_a, check=True,
        )
        tag_sha = subprocess.run(
            ["git", "rev-parse", "baseline-tag^{tag}"], cwd=self.checkout_a,
            text=True, capture_output=True, check=True,
        ).stdout.strip()

        self.assertTrue(scr.verify_baseline_commit(str(self.checkout_a), commit_sha))
        self.assertFalse(scr.verify_baseline_commit(str(self.checkout_a), tag_sha))

        foreign = self.root / "foreign"
        foreign.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=foreign, check=True)
        subprocess.run(
            ["git", "config", "user.email", "tests@example.com"], cwd=foreign, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Aru Tests"], cwd=foreign, check=True,
        )
        (foreign / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        subprocess.run(["git", "add", "foreign.txt"], cwd=foreign, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "foreign"], cwd=foreign, check=True)
        foreign_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=foreign,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        git_envs = (
            {"GIT_DIR": str(foreign / ".git")},
            {"GIT_OBJECT_DIRECTORY": str(foreign / ".git" / "objects")},
            {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(foreign / ".git" / "objects")},
        )
        for inherited in git_envs:
            with self.subTest(inherited=inherited), patch.dict(os.environ, inherited):
                self.assertFalse(
                    scr.verify_baseline_commit(str(self.checkout_a), foreign_sha)
                )

        subprocess.run(
            ["git", "update-ref", f"refs/replace/{tag_sha}", commit_sha],
            cwd=self.checkout_a, check=True,
        )
        self.assertFalse(scr.verify_baseline_commit(str(self.checkout_a), tag_sha))

    def test_concurrent_same_event_creates_one_github_record_and_transition(self):
        store = DeliveryIncrementStore(self.root / "increments.json")
        calls = []
        guard = threading.Lock()

        def recorder(*args):
            with guard:
                calls.append(args)
            time.sleep(0.02)
            return "https://github.com/owner/checkout-a/issues/300#issuecomment-1"

        payload = self.payload(
            event_id="concurrent-sprint",
            text=(
                "sprint authorize control #300 issues #10 baseline " + "a" * 40
            ),
        )

        def deliver():
            return scr.handle_slack_message(
                sample_config(), self.registry, payload, set(),
                notify=lambda *_args: {"ok": True},
                seen_path=self.seen_path,
                increment_store=store,
                baseline_verifier=lambda *_args: True,
                record_increment=recorder,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(lambda _index: deliver(), range(2)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(store.list()), 1)
        self.assertEqual(sum(reply is not None for reply in replies), 1)

    def test_bounded_runner_turns_timeout_into_failure(self):
        with patch.object(
            scr.subprocess, "run",
            side_effect=scr.subprocess.TimeoutExpired(["gh"], 1),
        ):
            self.assertEqual(
                scr._run_bounded(["gh"], cwd=str(self.checkout_a), timeout=1)[0],
                124,
            )

    def test_github_decision_comment_is_idempotent_by_event_marker(self):
        payload = {
            "decision_id": "proj_alpha|T01234567|C01234567|evt-1",
            "github_repository": "owner/repo",
        }
        marker = hashlib.sha256(payload["decision_id"].encode()).hexdigest()[:20]
        existing_url = "https://github.com/owner/repo/issues/300#issuecomment-8"
        existing_payload = dict(payload)
        existing = json.dumps({
            "comments": [{
                "body": (
                    f"<!-- aru-delivery-decision:v1:{marker} -->\n"
                    f"```json\n{json.dumps(existing_payload, indent=2, sort_keys=True)}\n```"
                ),
                "url": existing_url,
            }]
        })
        with patch.dict(os.environ, {
            "GH_REPO": "other/repo", "GH_HOST": "example.invalid",
            "GIT_DIR": str(self.root / "other.git"),
        }), patch.object(scr, "_run_bounded", return_value=(0, existing, "")) as run:
            self.assertEqual(
                scr.github_increment_decision(300, payload, str(self.checkout_a)),
                existing_url,
            )
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("--repo", command)
        self.assertIn("github.com/owner/repo", command)
        self.assertNotIn("GH_REPO", run.call_args.kwargs["env"])
        self.assertNotIn("GH_HOST", run.call_args.kwargs["env"])
        self.assertNotIn("GIT_DIR", run.call_args.kwargs["env"])

        duplicate_payload = (
            f"<!-- aru-delivery-decision:v1:{marker} -->\n"
            "```json\n"
            '{"decision_id":"wrong","decision_id":"proj_alpha|T01234567|C01234567|evt-1",'
            '"github_repository":"owner/repo"}\n```'
        )
        duplicate_marker = (
            f"<!-- aru-delivery-decision:v1:{marker} -->\n"
            f"<!-- aru-delivery-decision:v1:{marker} -->\n"
            f"```json\n{json.dumps(payload)}\n```"
        )
        for body in (duplicate_payload, duplicate_marker):
            response = json.dumps({
                "comments": [{"body": body, "url": existing_url}],
            })
            with self.subTest(body=body), patch.object(
                scr, "_run_bounded", return_value=(0, response, ""),
            ):
                self.assertIsNone(
                    scr.github_increment_decision(300, payload, str(self.checkout_a))
                )

        with patch.object(
            scr, "_run_bounded",
            side_effect=[
                (0, '{"comments": []}', ""),
                (0, "https://github.com/owner/repo/issues/300#issuecomment-9\n", ""),
            ],
        ) as run:
            created = scr.github_increment_decision(300, payload, str(self.checkout_a))
        self.assertTrue(created.endswith("issuecomment-9"))
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertIn("github.com/owner/repo", call.args[0])

    def test_wrong_operator_cannot_create_sprint_or_github_record(self):
        store = DeliveryIncrementStore(self.root / "increments.json")
        calls = []
        reply = scr.handle_slack_message(
            sample_config(), self.registry,
            self.payload(
                user="U99999999",
                text=(
                    "sprint authorize control #300 issues #10 baseline " + "d" * 40
                ),
            ),
            set(),
            seen_path=self.seen_path,
            increment_store=store,
            baseline_verifier=lambda *_args: True,
            record_increment=lambda *args: calls.append(args),
        )
        self.assertIsNone(reply)
        self.assertEqual(calls, [])
        self.assertEqual(store.list(), [])

    def test_wrong_project_transition_fails_before_github_record(self):
        store = DeliveryIncrementStore(self.root / "increments.json")
        authorize_payload = self.payload(
            event_id="project-a-auth",
            text=(
                "sprint authorize control #300 issues #10 baseline " + "e" * 40
            ),
        )
        authorized = scr.handle_slack_message(
            sample_config(), self.registry, authorize_payload, set(),
            notify=lambda *_args: {"ok": True},
            seen_path=self.seen_path,
            increment_store=store,
            baseline_verifier=lambda *_args: True,
            record_increment=lambda *_args: (
                "https://github.com/owner/checkout-a/issues/300#issuecomment-1"
            ),
        )
        increment_id = store.list(self.project_a.project_id)[0]["increment_id"]
        self.assertIn(increment_id, authorized)

        calls = []
        wrong_project = self.payload(
            channel="C11111111",
            event_id="project-b-start",
            text=f"sprint start {increment_id}",
        )
        reply = scr.handle_slack_message(
            sample_config(), self.registry, wrong_project, set(),
            notify=lambda *_args: {"ok": True},
            seen_path=self.root / "project-b-seen.json",
            increment_store=store,
            baseline_verifier=lambda *_args: True,
            record_increment=lambda *args: calls.append(args),
        )
        self.assertIn("another project", reply)
        self.assertEqual(calls, [])
        self.assertEqual(store.get(increment_id)["lifecycle_state"], "authorized")

    def test_claim_merge_review_verbs_are_refused_without_github_mutation(self):
        for verb in ("claim", "merge", "review"):
            parsed = scr.parse_command(f"<@U999> {verb} 181")
            self.assertEqual(parsed["verb"], "refused-queue")
            self.assertEqual(parsed["refused"], verb)
        comments = []
        reply = scr.handle_command(
            sample_config(),
            {"verb": "refused-queue", "refused": "merge"},
            self.project_a,
            comment=lambda *args: comments.append(args) or True,
        )
        self.assertIn("not a work queue", reply)
        self.assertEqual(comments, [])

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
        user_id = "U99999999"
        reply = scr.handle_slack_message(
            sample_config(), self.registry, self.payload(user=user_id), set(),
            seen_path=self.seen_path,
        )
        self.assertIsNone(reply)
        self.assertFalse(self.seen_path.exists())
        persisted = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(user_id, persisted)
        self.assertEqual(json.loads(persisted)["events"][-1]["action"], "unauthorized_inbound")

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
        keys = json.loads(self.seen_path.read_text(encoding="utf-8"))["ids"]
        self.assertIn("proj_checkout_a|T01234567|C01234567|evt-1", keys)

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

    def test_legacy_stop_file_adoption_uses_portable_fchmod(self):
        self.stop_path.write_text('{"projects": []}\n', encoding="utf-8")
        self.stop_path.chmod(0o644)
        with patch.object(os, "chmod", side_effect=NotImplementedError) as chmod, \
             patch.object(os, "fchmod", wraps=os.fchmod) as secured:
            scr.load_stop_file(self.stop_path)
        chmod.assert_not_called()
        secured.assert_called()
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

    def test_agent_scoped_stop_and_resume_are_rejected_without_state_changes(self):
        self.assertIn(
            "not supported in V1",
            scr.apply_stop(self.project_a.local_path, "cursor-1", self.stop_path),
        )
        self.assertFalse(self.stop_path.exists())
        self.assertIn(
            "not supported in V1",
            scr.apply_resume(self.project_a.local_path, "cursor-1", self.stop_path),
        )
        self.assertFalse(self.stop_path.exists())

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

    def test_identity_verification_error_does_not_consume_retry(self):
        with patch.object(
            scr, "verified_runtime_health",
            side_effect=scr.RegistryError("identity lookup timed out"),
        ):
            reply = scr.handle_slack_message(
                sample_config(), self.registry,
                self.payload(event_id="retry-after-recovery"), set(),
                seen_path=self.seen_path,
            )
        self.assertIsNone(reply)
        self.assertFalse(self.seen_path.exists())

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

    def test_corrupt_seen_store_returns_operator_visible_failure(self):
        self.seen_path.write_text("{broken", encoding="utf-8")
        self.seen_path.chmod(0o600)
        replies = []
        reply = scr.handle_slack_message(
            sample_config(), self.registry, self.payload(event_id="corrupt-store"), set(),
            notify=lambda _config, event: replies.append(event["text"]) or {"ok": True},
            seen_path=self.seen_path,
        )
        self.assertIn("state is unusable", reply)
        self.assertEqual(replies, [reply])
        self.assertEqual(self.seen_path.read_text(encoding="utf-8"), "{broken")

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

    def test_stop_bridge_handles_process_exit_during_signal(self):
        with patch.object(scr, "PID_PATH", self.root / "bridge.pid"), \
             patch.object(scr, "_pid_record", return_value={"pid": 123}), \
             patch.object(scr, "_pid_exists", return_value=True), \
             patch.object(scr, "_is_our_bridge", return_value=True), \
             patch.object(os, "kill", side_effect=ProcessLookupError), \
             patch.object(scr, "clear_pid") as clear:
            scr.PID_PATH.write_text("{}", encoding="utf-8")
            self.assertEqual(scr.stop_bridge(), "bridge is not running")
        clear.assert_called_once()

    def test_stop_bridge_reports_signal_permission_failure(self):
        with patch.object(scr, "PID_PATH", self.root / "bridge.pid"), \
             patch.object(scr, "_pid_record", return_value={"pid": 123}), \
             patch.object(scr, "_pid_exists", return_value=True), \
             patch.object(scr, "_is_our_bridge", return_value=True), \
             patch.object(os, "kill", side_effect=PermissionError("denied")):
            scr.PID_PATH.write_text("{}", encoding="utf-8")
            self.assertIn("could not signal pid 123", scr.stop_bridge())
            self.assertTrue(scr.PID_PATH.exists())

    def test_start_refuses_missing_operator_before_opening_bridge(self):
        code = scr.start_bridge(sample_config(operator_user_id=""), self.registry)
        self.assertEqual(code, 1)

    def test_start_refuses_registry_from_another_workspace(self):
        code = scr.start_bridge(sample_config(team_id="T99999999"), self.registry)
        self.assertEqual(code, 1)


class EpicSplitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def _story(self, **overrides):
        story = {
            "title": "feat(ops): slice the epic",
            "summary": "Do the slice.",
            "depends_on": [178],
            "touches": ["scripts/slack_control_room.py"],
            "parallel_eligible": False,
            "acceptance_criteria": ["- [ ] Helper files one child issue"],
            "type": "feat",
        }
        story.update(overrides)
        return story

    def _ready_story(self, **overrides):
        return self._story(
            depends_on=[],
            acceptance_criteria=[
                "- [ ] Helper files one child issue "
                "(verify: `python3 -m unittest tests.test_slack_control_room`)"
            ],
            decision_boundaries=[
                "- Default: file children with Epic: #N, never depends-on the parent epic",
                "- Error handling: exit 1 on an invalid split document",
            ],
            non_goals=["- Treating Slack as a work queue"],
            verification="`python3 -m unittest tests.test_slack_control_room` exits 0.",
            **overrides,
        )

    def _write_split(self, payload, mode=0o600):
        path = self.root / "split.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_slack_event_payload_is_not_a_filing_source(self):
        payload = {
            "type": "message",
            "text": ":+1: file these issues",
            "client_msg_id": "evt-9",
            "channel": "C01234567",
        }
        calls = []
        with self.assertRaisesRegex(scr.SplitError, "conversation"):
            scr.file_epic_split(
                payload, 178, str(self.repo),
                run_cmd_fn=lambda *args, **kwargs: calls.append(args) or (0, "", ""),
            )
        self.assertEqual(calls, [])

    def test_thumbs_up_slack_message_does_not_claim_or_merge(self):
        self.assertIsNone(scr.parse_command(":+1: looks good, file children of #178"))
        self.assertIsNone(scr.parse_command("file-split --epic 178"))
        comments = []
        reply = scr.handle_command(
            sample_config(),
            {"verb": "refused-queue", "refused": "merge"},
            type("P", (), {"project_id": "proj_x", "healthy": True})(),
            comment=lambda *args: comments.append(args) or True,
        )
        self.assertIn("not a work queue", reply)
        self.assertEqual(comments, [])

    def test_dry_run_validates_without_github_mutations(self):
        calls = []
        result = scr.file_epic_split(
            {"epic": 178, "stories": [self._story()]},
            178, str(self.repo), dry_run=True,
            run_cmd_fn=lambda *a, **k: calls.append(a) or (0, "", ""),
        )
        self.assertEqual(calls, [])
        self.assertTrue(result["dry_run"])
        self.assertIn("Epic: #178", result["filed"][0]["body"])
        self.assertNotIn("depends-on: #178", result["filed"][0]["body"])
        self.assertEqual(result["filed"][0]["status"], "Backlog")
        self.assertIsNone(result["filed"][0]["number"])

    def test_file_split_creates_board_item_and_comments_epic(self):
        comments = []

        def run_cmd(cmd, check=False, cwd=None):
            if cmd[:3] == ["gh", "issue", "view"]:
                return 0, json.dumps({
                    "state": "OPEN",
                    "labels": [{"name": "type:epic"}],
                }), ""
            if cmd[:3] == ["gh", "issue", "create"]:
                return 0, "https://github.com/o/r/issues/310\n", ""
            if any(str(part).endswith("update_issue_status.py") for part in cmd):
                return 0, "attached", ""
            raise AssertionError(cmd)

        result = scr.file_epic_split(
            {"epic": 178, "stories": [self._story()]},
            178, str(self.repo),
            run_cmd_fn=run_cmd,
            comment_fn=lambda number, body, root: comments.append((number, body, root)) or True,
        )
        self.assertEqual(result["filed"][0]["number"], 310)
        self.assertEqual(comments[0][0], 178)
        self.assertIn("#310", comments[0][1])
        self.assertIn("Epic: #178", result["filed"][0]["body"])
        self.assertIn("depends-on: none", result["filed"][0]["body"])
        self.assertNotIn("depends-on: #178", result["filed"][0]["body"])

    def test_incomplete_story_lands_in_backlog(self):
        story = self._story(touches=[], acceptance_criteria=[])
        parsed = scr.validate_split_story(story, 178)
        self.assertEqual(scr.story_board_status(parsed), "Backlog")

    def test_parent_epic_is_stripped_from_depends_on(self):
        parsed = scr.validate_split_story(self._story(depends_on=[178, 172]), 178)
        self.assertEqual(parsed["depends_on"], [172])
        self.assertNotIn("depends-on: #178", scr.render_split_issue_body(parsed, 178))

    def test_fully_specified_story_is_ready(self):
        parsed = scr.validate_split_story(self._ready_story(), 178)
        self.assertEqual(scr.story_board_status(parsed), "Ready")
        body = scr.render_split_issue_body(parsed, 178)
        self.assertIn("## Decision Boundaries", body)
        self.assertIn("## Non-Goals", body)
        self.assertIn("## Verification", body)

    def test_specified_without_ready_contract_stays_backlog(self):
        parsed = scr.validate_split_story(self._story(), 178)
        self.assertTrue(parsed["touches"])
        self.assertTrue(parsed["acceptance_criteria"])
        self.assertEqual(scr.story_board_status(parsed), "Backlog")

    def test_file_split_notifies_slack_thread(self):
        notes = []

        def run_cmd(cmd, check=False, cwd=None):
            if cmd[:3] == ["gh", "issue", "view"]:
                return 0, json.dumps({
                    "state": "OPEN",
                    "labels": [{"name": "type:epic"}],
                }), ""
            if cmd[:3] == ["gh", "issue", "create"]:
                return 0, "https://github.com/o/r/issues/310\n", ""
            if any(str(part).endswith("update_issue_status.py") for part in cmd):
                return 0, "attached", ""
            raise AssertionError(cmd)

        def notify(config, event, thread_ts=None, **kwargs):
            notes.append((config.channel_id, event["text"], thread_ts))
            return {"ok": True}

        result = scr.file_epic_split(
            {"epic": 178, "stories": [self._story()]},
            178, str(self.repo),
            run_cmd_fn=run_cmd,
            comment_fn=lambda *args: True,
            notify_fn=notify,
            slack_config=sample_config(channel_id="C01234567"),
            thread_ts="123.456",
        )
        self.assertEqual(result["filed"][0]["number"], 310)
        self.assertEqual(notes, [("C01234567", result["summary"], "123.456")])
        self.assertIn("#310", notes[0][1])

    def test_main_file_split_wires_thread_notify(self):
        path = self._write_split({"epic": 178, "stories": [self._story()]})
        captured = {}

        def fake_split(*args, **kwargs):
            captured.update(kwargs)
            return {"epic": 178, "dry_run": False, "filed": [], "summary": "ok"}

        cfg = sample_config(channel_id="C01234567")
        with patch.object(scr, "file_epic_split", fake_split), \
             patch.object(scr, "config_from_env", return_value=cfg), \
             patch.object(scr, "load_slack_env", return_value={}):
            code = scr.main([
                "file-split", "--epic", "178", "--from-file", str(path),
                "--repo-dir", str(self.repo), "--thread-ts", "99.1",
            ])
        self.assertEqual(code, 0)
        self.assertIs(captured["notify_fn"], scr.post_event)
        self.assertEqual(captured["slack_config"], cfg)
        self.assertEqual(captured["thread_ts"], "99.1")

    def test_main_file_split_skips_notify_when_slack_unconfigured(self):
        path = self._write_split({"epic": 178, "stories": [self._story()]})
        captured = {}

        def fake_split(*args, **kwargs):
            captured.update(kwargs)
            return {"epic": 178, "dry_run": False, "filed": [], "summary": "ok"}

        with patch.object(scr, "file_epic_split", fake_split), \
             patch.object(scr, "config_from_env", side_effect=ValueError("no slack")):
            code = scr.main([
                "file-split", "--epic", "178", "--from-file", str(path),
                "--repo-dir", str(self.repo), "--thread-ts", "99.1",
            ])
        self.assertEqual(code, 0)
        self.assertIsNone(captured["notify_fn"])
        self.assertIsNone(captured["slack_config"])
        self.assertEqual(captured["thread_ts"], "99.1")

    def test_world_readable_split_file_is_rejected(self):
        path = self._write_split({"epic": 178, "stories": [self._story()]}, mode=0o644)
        with self.assertRaisesRegex(scr.SplitError, "0600"):
            scr.load_split_file(path)

    def test_cli_dry_run_reads_secure_file(self):
        path = self._write_split({"epic": 178, "stories": [self._story()]})
        code = scr.main([
            "file-split", "--epic", "178", "--from-file", str(path),
            "--repo-dir", str(self.repo), "--dry-run",
        ])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
