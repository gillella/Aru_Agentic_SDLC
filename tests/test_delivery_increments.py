# line-ceiling: 550
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from delivery_increments import (  # noqa: E402
    DeliveryIncrementStore,
    IncrementError,
    increment_id_for_event,
    operator_evidence,
)
import init_project  # noqa: E402


SHA = "a" * 40


class IncrementFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.store = DeliveryIncrementStore(self.root / "increments.json")
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def mutate_file(self, updater):
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        updater(payload)
        self.store.path.write_text(json.dumps(payload), encoding="utf-8")
        self.store.path.chmod(0o600)

    def evidence(self, event=None, url=None, control=300):
        self.counter += 1
        event_id = event or f"evt-{self.counter}"
        return operator_evidence(
            user_id="U01234567",
            team_id="T01234567",
            channel_id="C01234567",
            event_id=event_id,
            github_repository="owner/repo",
            github_record_url=url or f"https://github.com/owner/repo/issues/{control}#issuecomment-{self.counter}",
            recorded_at=f"2026-08-15T12:{self.counter:02d}:00Z",
        )

    def authorize(
        self, *, project="proj_alpha", event="authorize-1", scope=None,
        kind="normal", control=300,
    ):
        increment_id = increment_id_for_event(project, event)
        decision = {
            "decision_id": f"{project}|T01234567|C01234567|{event}",
            "action": "authorize",
            "increment_id": increment_id,
            "project_id": project,
            "operator_user_id": "U01234567",
            "github_repository": "owner/repo",
            "kind": kind,
            "control_issue": control,
            "issue_scope": scope or [10, 11],
            "baseline_commit": SHA,
        }
        return self.store.apply_operator_decision(
            decision, self.evidence(event=event, control=control)
        ), decision

    def transition(self, record, action, *, event=None, **extra):
        event_id = event or f"{action}-{self.counter + 1}"
        decision = {
            "decision_id": f"{record['project_id']}|T01234567|C01234567|{event_id}",
            "action": action,
            "increment_id": record["increment_id"],
            "project_id": record["project_id"],
            "operator_user_id": "U01234567",
            "github_repository": "owner/repo",
            **extra,
        }
        return self.store.apply_operator_decision(
            decision,
            self.evidence(event=event_id, control=record["control_issue"]),
        )


class DeliveryIncrementSchemaTests(IncrementFixture):
    def test_record_has_immutable_identity_scope_baseline_evidence_and_separate_release(self):
        record, _ = self.authorize(scope=[12, 10])
        self.assertEqual(record["issue_scope"], [10, 12])
        self.assertEqual(record["baseline_commit"], SHA)
        self.assertEqual(record["lifecycle_state"], "authorized")
        self.assertEqual(record["release_state"], "unreleased")
        self.assertEqual(record["decisions"][0]["evidence"]["source"], "slack_control_room")
        self.assertEqual(self.store.get(record["increment_id"]), record)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

        copy = self.store.get(record["increment_id"])
        copy["increment_id"] = "inc_00000000000000000000"
        self.assertEqual(self.store.get(record["increment_id"])["increment_id"], record["increment_id"])

    def test_corrupt_or_ambiguous_registry_fails_closed(self):
        self.store.path.write_text('{"schema": 1, "increments": [{"broken": true}]}')
        self.store.path.chmod(0o600)
        with self.assertRaises(IncrementError):
            self.store.list()

        for document in (
            '{"schema": 2, "schema": 1, "increments": []}',
            '{"schema": 1, "increments": [{"broken": true}], "increments": []}',
        ):
            self.store.path.write_text(document, encoding="utf-8")
            self.store.path.chmod(0o600)
            with self.subTest(document=document), self.assertRaises(IncrementError):
                self.store.list()

        self.store.path.write_bytes(b'{"schema": 1, "increments": []}\xff')
        self.store.path.chmod(0o600)
        with self.assertRaises(IncrementError):
            self.store.list()

    def test_schema_boolean_and_list_valued_fields_fail_closed(self):
        self.authorize()
        self.mutate_file(lambda payload: payload.__setitem__("schema", True))
        with self.assertRaises(IncrementError):
            self.store.list()

        self.store = DeliveryIncrementStore(self.root / "list-fields.json")
        self.counter = 0
        self.authorize(event="fresh", project="proj_beta")
        self.mutate_file(
            lambda payload: payload["increments"][0]["decisions"][0]["decision"].__setitem__(
                "action", []
            )
        )
        with self.assertRaises(IncrementError):
            self.store.list()

        valid_store = DeliveryIncrementStore(self.root / "valid-project-filter.json")
        with self.assertRaises(IncrementError):
            valid_store.list([])

        self.store = DeliveryIncrementStore(self.root / "float-schema.json")
        self.store.path.write_text('{"schema": 1.0, "increments": []}')
        self.store.path.chmod(0o600)
        with self.assertRaises(IncrementError):
            self.store.list()

    def test_numeric_baseline_and_ambiguous_slack_authority_fail_closed(self):
        _, decision = self.authorize()
        numeric = dict(decision, baseline_commit=int("1" * 40))
        other_store = DeliveryIncrementStore(self.root / "numeric-baseline.json")
        with self.assertRaises(IncrementError):
            other_store.apply_operator_decision(numeric, self.evidence(event="numeric"))

        for overrides in (
            {"user_id": "U"},
            {"team_id": "T01234567|C2"},
            {"channel_id": "C01234567|C3"},
        ):
            values = {
                "user_id": "U01234567",
                "team_id": "T01234567",
                "channel_id": "C01234567",
                "event_id": "authority",
                "github_repository": "owner/repo",
                "github_record_url": (
                    "https://github.com/owner/repo/issues/300#issuecomment-1"
                ),
                "recorded_at": "2026-08-15T12:00:00Z",
                **overrides,
            }
            with self.subTest(overrides=overrides), self.assertRaises(IncrementError):
                operator_evidence(**values)

    def test_lifecycle_fields_are_derived_from_history(self):
        self.authorize()

        def forge(payload):
            record = payload["increments"][0]
            record["lifecycle_state"] = "accepted"
            record["accepted_at"] = record["updated_at"]

        self.mutate_file(forge)
        with self.assertRaisesRegex(IncrementError, "replayed decision history"):
            self.store.list()

    def test_operator_evidence_must_point_to_the_increment_control_issue(self):
        increment_id = increment_id_for_event("proj_alpha", "wrong-anchor")
        decision = {
            "decision_id": "proj_alpha|T01234567|C01234567|wrong-anchor",
            "action": "authorize",
            "increment_id": increment_id,
            "project_id": "proj_alpha",
            "operator_user_id": "U01234567",
            "github_repository": "owner/repo",
            "kind": "normal",
            "control_issue": 300,
            "issue_scope": [10],
            "baseline_commit": SHA,
        }
        with self.assertRaisesRegex(IncrementError, "wrong control issue"):
            self.store.apply_operator_decision(
                decision, self.evidence(event="wrong-anchor", control=999)
            )

    def test_generated_id_is_stable_for_the_same_project_event(self):
        first = increment_id_for_event("proj_alpha", "evt-1")
        self.assertEqual(first, increment_id_for_event("proj_alpha", "evt-1"))
        self.assertNotEqual(first, increment_id_for_event("proj_alpha", "evt-2"))

    def test_new_boards_include_increment_identity_and_state_fields(self):
        fields = {"fields": [{"id": "status-id", "name": "Status"}]}
        views = {"data": {"node": {"views": {"nodes": []}}}}
        with patch.object(init_project, "run_gh_json", side_effect=[fields, views]), \
             patch.object(init_project, "run_cmd", return_value=(0, "", "")) as run:
            self.assertTrue(init_project.configure_board(5, "owner", "project-id"))
        field_commands = [
            call.args[0] for call in run.call_args_list
            if call.args[0][:3] == ["gh", "project", "field-create"]
        ]
        self.assertTrue(any("Delivery Increment" in command for command in field_commands))
        self.assertTrue(any("Increment State" in command for command in field_commands))


class SlackAuthorizationTests(IncrementFixture):
    def test_non_slack_or_non_durable_evidence_cannot_authorize(self):
        _, decision = self.authorize()
        other_store = DeliveryIncrementStore(self.root / "other.json")
        weak = self.evidence()
        weak["source"] = "github_comment"
        with self.assertRaisesRegex(IncrementError, "authenticated Slack"):
            other_store.apply_operator_decision(decision, weak)
        weak = self.evidence()
        weak["github_record_url"] = ""
        with self.assertRaisesRegex(IncrementError, "GitHub"):
            other_store.apply_operator_decision(decision, weak)
        self.assertFalse(other_store.path.exists())

    def test_duplicate_slack_decision_is_idempotent_but_changed_replay_fails(self):
        record, decision = self.authorize(event="same-event")
        evidence = record["decisions"][0]["evidence"]
        repeated = self.store.apply_operator_decision(decision, evidence)
        self.assertEqual(repeated, record)
        changed = dict(decision, issue_scope=[99])
        with self.assertRaisesRegex(IncrementError, "reused"):
            self.store.apply_operator_decision(changed, evidence)

    def test_decision_identity_operator_and_repository_bind_to_evidence(self):
        _, decision = self.authorize()
        other_store = DeliveryIncrementStore(self.root / "authority.json")
        evidence = self.evidence(event="authority")
        mismatched = dict(
            decision,
            decision_id="proj_alpha|T99999999|C99999999|other-event",
        )
        with self.assertRaisesRegex(IncrementError, "decision identity"):
            other_store.apply_operator_decision(mismatched, evidence)
        mismatched = dict(
            decision,
            decision_id="proj_alpha|T01234567|C01234567|authority",
            github_repository="other/repo",
        )
        with self.assertRaisesRegex(IncrementError, "authority"):
            other_store.apply_operator_decision(mismatched, evidence)

    def test_risk_accepted_field_is_rejected_on_non_accept_decisions(self):
        record, _ = self.authorize()
        with self.assertRaisesRegex(IncrementError, "only for sprint acceptance"):
            self.transition(record, "start", risk_accepted=False)


class ScopeFreezeTests(IncrementFixture):
    def test_scope_is_frozen_until_a_new_operator_decision(self):
        record, _ = self.authorize(scope=[10, 11])
        detached = self.store.get(record["increment_id"])
        detached["issue_scope"].append(99)
        self.assertEqual(self.store.get(record["increment_id"])["issue_scope"], [10, 11])

        revised = self.transition(record, "revise", issue_scope=[10, 12])
        self.assertEqual(revised["issue_scope"], [10, 12])
        self.assertEqual(revised["decisions"][0]["scope_after"], [10, 11])
        self.assertEqual(revised["decisions"][1]["scope_after"], [10, 12])

    def test_scope_cannot_change_after_acceptance(self):
        record, _ = self.authorize()
        record = self.transition(record, "start")
        record = self.transition(record, "accept")
        with self.assertRaisesRegex(IncrementError, "authorized or active"):
            self.transition(record, "revise", issue_scope=[77])

    def test_non_revise_scope_change_and_out_of_order_decision_fail_closed(self):
        record, _ = self.authorize(scope=[10])
        record = self.transition(record, "start")

        def forge(payload):
            stored = payload["increments"][0]
            stored["issue_scope"] = [999]
            stored["decisions"][-1]["scope_after"] = [999]

        self.mutate_file(forge)
        with self.assertRaisesRegex(IncrementError, "without a revise"):
            self.store.list()

        self.store = DeliveryIncrementStore(self.root / "chronology.json")
        self.counter = 0
        record, _ = self.authorize(scope=[10])
        record = self.transition(record, "start")
        record = self.transition(record, "revise", issue_scope=[20])
        older = {
            "decision_id": "proj_alpha|T01234567|C01234567|late-delivery",
            "action": "revise",
            "increment_id": record["increment_id"],
            "project_id": "proj_alpha",
            "operator_user_id": "U01234567",
            "github_repository": "owner/repo",
            "issue_scope": [30],
        }
        evidence = operator_evidence(
            user_id="U01234567", team_id="T01234567", channel_id="C01234567",
            event_id="late-delivery", github_repository="owner/repo",
            github_record_url="https://github.com/owner/repo/issues/300#issuecomment-old",
            recorded_at="2026-08-15T12:02:30Z",
        )
        with self.assertRaisesRegex(IncrementError, "strictly chronological"):
            self.store.apply_operator_decision(older, evidence)


class ProjectConcurrencyTests(IncrementFixture):
    def test_one_project_has_at_most_one_active_normal_increment(self):
        first, _ = self.authorize(event="first")
        with self.assertRaisesRegex(IncrementError, "already has an active increment"):
            self.authorize(event="second", scope=[20])
        self.assertEqual(self.store.active("proj_alpha")["increment_id"], first["increment_id"])

    def test_accepted_release_lane_does_not_remain_the_active_work_lane(self):
        first, _ = self.authorize(event="first")
        first = self.transition(first, "start")
        self.transition(first, "accept")
        self.assertIsNone(self.store.active("proj_alpha"))
        second, _ = self.authorize(event="second", scope=[20])
        self.assertEqual(self.store.active("proj_alpha")["increment_id"], second["increment_id"])

    def test_concurrent_authorizations_are_lock_serialized(self):
        def attempt(index):
            event = f"concurrent-{index}"
            decision = {
                "decision_id": f"proj_alpha|T01234567|C01234567|{event}",
                "action": "authorize",
                "increment_id": increment_id_for_event("proj_alpha", event),
                "project_id": "proj_alpha",
                "operator_user_id": "U01234567",
                "github_repository": "owner/repo",
                "kind": "normal",
                "control_issue": 300,
                "issue_scope": [index],
                "baseline_commit": SHA,
            }
            evidence = operator_evidence(
                user_id="U01234567", team_id="T01234567", channel_id="C01234567",
                event_id=event,
                github_repository="owner/repo",
                github_record_url=(
                    f"https://github.com/owner/repo/issues/300#issuecomment-{index}"
                ),
                recorded_at=f"2026-08-15T13:00:0{index}Z",
            )
            try:
                self.store.apply_operator_decision(decision, evidence)
                return "created"
            except IncrementError:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, (1, 2)))
        self.assertEqual(sorted(outcomes), ["blocked", "created"])
        self.assertEqual(len(self.store.list("proj_alpha")), 1)

    def test_document_rejects_two_tampered_active_normal_increments(self):
        self.authorize(event="normal", scope=[10])
        self.authorize(event="emergency", scope=[99], kind="emergency", control=301)

        def forge(payload):
            emergency = payload["increments"][1]
            emergency["kind"] = "normal"
            emergency["decisions"][0]["decision"]["kind"] = "normal"

        self.mutate_file(forge)
        with self.assertRaisesRegex(IncrementError, "multiple active normal"):
            self.store.list()


class ReleaseQueueTests(IncrementFixture):
    def test_acceptance_and_deployment_are_separate_authorizations(self):
        record, _ = self.authorize()
        record = self.transition(record, "start")
        record = self.transition(record, "accept")
        self.assertEqual(record["release_state"], "unreleased")
        with self.assertRaisesRegex(IncrementError, "explicit authorization"):
            self.transition(record, "deployed")
        record = self.transition(record, "authorize-deployment")
        self.assertEqual(record["lifecycle_state"], "accepted")
        record = self.transition(record, "deployed")
        self.assertEqual(record["lifecycle_state"], "closed")
        self.assertEqual(record["release_state"], "deployed")

    def test_second_accepted_undeployed_increment_needs_risk_acceptance(self):
        first, _ = self.authorize(event="first")
        first = self.transition(first, "start")
        self.transition(first, "accept")
        second, _ = self.authorize(event="second", scope=[20])
        second = self.transition(second, "start")
        with self.assertRaisesRegex(IncrementError, "risk acceptance"):
            self.transition(second, "accept")
        accepted = self.transition(second, "accept", risk_accepted=True)
        self.assertEqual(accepted["lifecycle_state"], "accepted")

        def remove_risk(payload):
            payload["increments"][1]["decisions"][-1]["decision"].pop("risk_accepted")

        self.mutate_file(remove_risk)
        with self.assertRaisesRegex(IncrementError, "queue lacks explicit risk"):
            self.store.list()


class EmergencyIncrementTests(IncrementFixture):
    def test_emergency_increment_never_rewrites_normal_scope(self):
        normal, _ = self.authorize(event="normal", scope=[10, 11])
        emergency, _ = self.authorize(
            event="emergency", scope=[99], kind="emergency", control=301
        )
        emergency = self.transition(emergency, "revise", issue_scope=[99, 100])
        self.assertEqual(emergency["kind"], "emergency")
        self.assertEqual(self.store.get(normal["increment_id"])["issue_scope"], [10, 11])
        self.assertEqual(self.store.active("proj_alpha")["increment_id"], normal["increment_id"])


class DirectoryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_private_directory_concurrent_creation_race(self):
        from delivery_increments import _private_directory

        target = self.root / "concurrent_inc_dir"
        original_mkdir = Path.mkdir
        first_call = [True]

        def racing_mkdir(path_self, *args, **kwargs):
            if path_self == target and first_call[0]:
                first_call[0] = False
                original_mkdir(path_self, *args, **kwargs)
                raise FileExistsError(f"File exists: {path_self}")
            return original_mkdir(path_self, *args, **kwargs)

        with patch.object(Path, "mkdir", side_effect=racing_mkdir, autospec=True):
            _private_directory(target)

        self.assertTrue(target.is_dir())
        mode = target.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_private_directory_threadpool_concurrency(self):
        from delivery_increments import _private_directory

        target = self.root / "multi_threaded_inc_dir"

        def create_dir(_):
            _private_directory(target)
            return True

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(create_dir, range(16)))

        self.assertTrue(all(results))
        self.assertTrue(target.is_dir())
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_private_directory_concurrent_conflict_with_file(self):
        from delivery_increments import IncrementError, _private_directory

        target = self.root / "conflicting_file"
        target.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(IncrementError):
            _private_directory(target)

    def test_read_increment_rejects_symlink_and_insecure_file(self):
        from delivery_increments import IncrementError, _read_increment_unlocked

        real_file = self.root / "real_inc.json"
        real_file.write_text(json.dumps({"schema_version": 1, "increments": {}}), encoding="utf-8")
        os.chmod(real_file, 0o600)

        symlink_file = self.root / "symlink_inc.json"
        symlink_file.symlink_to(real_file)

        with self.assertRaises(IncrementError):
            _read_increment_unlocked(symlink_file, {})

        insecure_file = self.root / "insecure_inc.json"
        insecure_file.write_text(json.dumps({"schema_version": 1, "increments": {}}), encoding="utf-8")
        os.chmod(insecure_file, 0o644)

        with self.assertRaises(IncrementError):
            _read_increment_unlocked(insecure_file, {})

    def test_read_increment_rejects_foreign_owner(self):
        from delivery_increments import IncrementError, _read_increment_unlocked

        real_file = self.root / "foreign_inc.json"
        real_file.write_text(json.dumps({"schema_version": 1, "increments": {}}), encoding="utf-8")
        os.chmod(real_file, 0o600)

        real_fstat = os.fstat
        def mock_fstat(fd):
            st = real_fstat(fd)
            return os.stat_result((
                st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                os.getuid() + 1000, st.st_gid, st.st_size,
                st.st_atime, st.st_mtime, st.st_ctime
            ))

        with patch("os.fstat", side_effect=mock_fstat):
            with self.assertRaises(IncrementError):
                _read_increment_unlocked(real_file, {})


if __name__ == "__main__":
    unittest.main()
