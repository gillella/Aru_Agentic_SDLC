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

    def evidence(self, event=None, url=None, control=300):
        self.counter += 1
        event_id = event or f"evt-{self.counter}"
        return operator_evidence(
            user_id="U01234567",
            team_id="T01234567",
            channel_id="C01234567",
            event_id=event_id,
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

    def test_operator_evidence_must_point_to_the_increment_control_issue(self):
        increment_id = increment_id_for_event("proj_alpha", "wrong-anchor")
        decision = {
            "decision_id": "proj_alpha|T01234567|C01234567|wrong-anchor",
            "action": "authorize",
            "increment_id": increment_id,
            "project_id": "proj_alpha",
            "kind": "normal",
            "control_issue": 300,
            "issue_scope": [10],
            "baseline_commit": SHA,
        }
        with self.assertRaisesRegex(IncrementError, "wrong control issue"):
            self.store.apply_operator_decision(
                decision, self.evidence(control=999)
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
                "kind": "normal",
                "control_issue": 300,
                "issue_scope": [index],
                "baseline_commit": SHA,
            }
            evidence = operator_evidence(
                user_id="U01234567", team_id="T01234567", channel_id="C01234567",
                event_id=event,
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


if __name__ == "__main__":
    unittest.main()
