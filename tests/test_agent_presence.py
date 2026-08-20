# line-ceiling: 660
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_presence as ap


class AgentPresenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "agent-presence.json"
        self.clock = {"now": datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)}
        self.store = ap.PresenceStore(
            self.path,
            clock=lambda: self.clock["now"],
            heartbeat_ttl_seconds=60,
        )
        self.project_a = self.root / "proj-a"
        self.project_b = self.root / "proj-b"
        self.project_a.mkdir()
        self.project_b.mkdir()

    def advance(self, seconds: int) -> None:
        self.clock["now"] = self.clock["now"] + timedelta(seconds=seconds)

    def test_one_agent_binds_to_one_project(self):
        self.store.register(
            agent_id="cursor-1",
            family="cursor",
            project_id="proj_alpha",
            checkout_path=str(self.project_a),
            capabilities=["review"],
            wake_evidence_supported=["github-recovery"],
        )
        with self.assertRaises(ap.PresenceError):
            self.store.register(
                agent_id="cursor-1",
                family="cursor",
                project_id="proj_beta",
                checkout_path=str(self.project_b),
            )
        again = self.store.register(
            agent_id="cursor-1",
            family="cursor",
            project_id="proj_alpha",
            checkout_path=str(self.project_a),
            availability="busy",
            role="implement",
        )
        self.assertEqual(again.availability, "busy")
        self.assertEqual(again.role, "implement")

    def test_availability_states_and_query(self):
        for state in sorted(ap.AVAILABILITY_STATES):
            record = self.store.register(
                agent_id=f"agent-{state}",
                family="openai",
                project_id="proj_alpha",
                checkout_path=str(self.project_a),
                availability=state,
            )
            self.assertEqual(record.availability, state)
        found = self.store.query_project(project_id="proj_alpha", expire=False)
        self.assertEqual(len(found), len(ap.AVAILABILITY_STATES))
        self.assertTrue(all(item.project_id == "proj_alpha" for item in found))

    def test_heartbeat_expiry_keeps_registration(self):
        self.store.register(
            agent_id="codex-1",
            family="openai",
            project_id="proj_alpha",
            checkout_path=str(self.project_a),
            availability="available",
        )
        self.advance(120)
        changed = self.store.expire_stale()
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].availability, "temporarily-offline")
        kept = self.store.get("codex-1")
        self.assertIsNotNone(kept)
        self.assertEqual(kept.availability, "temporarily-offline")
        self.assertEqual(kept.project_id, "proj_alpha")
        returned = self.store.heartbeat("codex-1")
        self.assertEqual(returned.availability, "returned")

    def test_project_a_cannot_affect_project_b(self):
        self.store.register(
            agent_id="cursor-a",
            family="cursor",
            project_id="proj_alpha",
            checkout_path=str(self.project_a),
            workload={"active_issues": 1},
        )
        self.store.register(
            agent_id="cursor-b",
            family="cursor",
            project_id="proj_beta",
            checkout_path=str(self.project_b),
            workload={"active_issues": 9},
        )
        only_a = self.store.query_project(checkout_path=str(self.project_a), expire=False)
        only_b = self.store.query_project(project_id="proj_beta", expire=False)
        self.assertEqual([item.agent_id for item in only_a], ["cursor-a"])
        self.assertEqual([item.agent_id for item in only_b], ["cursor-b"])
        self.assertEqual(only_a[0].workload["active_issues"], 1)
        self.assertEqual(only_b[0].workload["active_issues"], 9)

    def test_same_product_different_projects_need_distinct_ids(self):
        self.store.register(
            agent_id="cursor-proj-a",
            family="cursor",
            project_id="proj_alpha",
            checkout_path=str(self.project_a),
        )
        self.store.register(
            agent_id="cursor-proj-b",
            family="cursor",
            project_id="proj_beta",
            checkout_path=str(self.project_b),
        )
        self.assertEqual(
            {item.agent_id for item in self.store.query_project(project_id="proj_alpha", expire=False)},
            {"cursor-proj-a"},
        )

    def test_path_derived_project_id_is_stable(self):
        first = ap.path_derived_project_id(self.project_a)
        second = ap.path_derived_project_id(self.project_a)
        other = ap.path_derived_project_id(self.project_b)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertTrue(ap.PROJECT_ID_RE.fullmatch(first))

    def test_identity_derived_id_is_clone_independent(self):
        identity = {
            "github_repo_id": "R_kgDOtest",
            "project_v2_id": "PVT_kwDOboard",
            "repo_slug": "acme/demo",
        }
        clone_a = self.root / "clones" / "agent-a"
        clone_b = self.root / "clones" / "agent-b"
        clone_a.mkdir(parents=True)
        clone_b.mkdir(parents=True)
        aru = self.root / "aru-home"
        aru.mkdir(mode=0o700)
        projects_path = aru / "projects.json"
        projects_path.write_text(
            json.dumps({"schema_version": 1, "projects": {}, "migrations": {}}) + "\n",
            encoding="utf-8",
        )
        os.chmod(projects_path, 0o600)
        first = ap.resolve_project_id(
            clone_a,
            projects_path=projects_path,
            identity_provider=lambda _path: identity,
        )
        second = ap.resolve_project_id(
            clone_b,
            projects_path=projects_path,
            identity_provider=lambda _path: identity,
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("proj_repo_"))
        self.assertNotEqual(first, ap.path_derived_project_id(clone_a))

    def test_unregister_allows_rebind(self):
        self.store.register(
            agent_id="cursor-1",
            family="cursor",
            project_id="proj_alpha",
            checkout_path=str(self.project_a),
        )
        self.store.unregister("cursor-1")
        rebound = self.store.register(
            agent_id="cursor-1",
            family="cursor",
            project_id="proj_beta",
            checkout_path=str(self.project_b),
        )
        self.assertEqual(rebound.project_id, "proj_beta")

    def test_cli_register_and_heartbeat(self):
        presence_path = self.root / "cli-presence.json"
        code = ap.main([
            "--path", str(presence_path),
            "register",
            "--agent", "cursor-1",
            "--family", "cursor",
            "--checkout", str(self.project_a),
            "--project-id", "proj_alpha",
        ])
        self.assertEqual(code, 0)
        store = ap.PresenceStore(presence_path)
        self.assertEqual(store.get("cursor-1").availability, "available")
        code = ap.main([
            "--path", str(presence_path),
            "heartbeat",
            "--agent", "cursor-1",
            "--availability", "busy",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(store.get("cursor-1").availability, "busy")

    def test_cli_wires_cooldown_metadata_for_all_presence_mutations(self):
        presence_path = self.root / "cli-cooldown-presence.json"
        first_retry = "2026-08-16T12:05:00Z"
        code = ap.main([
            "--path", str(presence_path), "register",
            "--agent", "cursor-1", "--family", "cursor",
            "--checkout", str(self.project_a), "--project-id", "proj_alpha",
            "--availability", "cooling-down",
            "--cooldown-reason", "rate-limited",
            "--cooldown-until", first_retry,
        ])
        self.assertEqual(code, 0)
        store = ap.PresenceStore(presence_path)
        self.assertEqual(store.get("cursor-1").cooldown_reason, "rate-limited")
        self.assertEqual(store.get("cursor-1").cooldown_until, first_retry)

        code = ap.main([
            "--path", str(presence_path), "heartbeat", "--agent", "cursor-1",
            "--availability", "cooling-down",
            "--cooldown-reason", "provider-outage",
            "--cooldown-until", "2026-08-16T12:10:00Z",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(store.get("cursor-1").cooldown_reason, "provider-outage")

        code = ap.main([
            "--path", str(presence_path), "set-availability",
            "--agent", "cursor-1", "--availability", "cooling-down",
            "--cooldown-reason", "credit-exhausted",
            "--cooldown-until", "2026-08-16T12:15:00Z",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(store.get("cursor-1").cooldown_reason, "credit-exhausted")

    def test_doctor_summary_is_read_only(self):
        aru = self.root / "aru-isolated"
        aru.mkdir(mode=0o700)
        projects_path = aru / "projects.json"
        projects_path.write_text(
            json.dumps({"schema_version": 1, "projects": {}, "migrations": {}}) + "\n",
            encoding="utf-8",
        )
        os.chmod(projects_path, 0o600)
        self.store.register(
            agent_id="cursor-cloud-1",
            family="cursor",
            project_id=ap.path_derived_project_id(self.project_a),
            checkout_path=str(self.project_a),
            availability="available",
        )
        self.advance(120)
        before = json.loads(self.path.read_text(encoding="utf-8"))
        agents = {"cursor": {}, "codex": {}, "claude": {}, "antigravity": {}}
        summary = ap.doctor_presence_summary(
            project=str(self.project_a),
            agents=agents,
            store=self.store,
            catalog_non_guarantees=["app quit"],
            projects_path=projects_path,
        )
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(before, after)
        self.assertEqual(self.store.get("cursor-cloud-1").availability, "available")
        self.assertIn("Presence never launches agents", " ".join(summary["wake_limitations"]))
        self.assertIn("app quit", " ".join(summary["wake_limitations"]))

    def test_doctor_picks_newest_heartbeat_by_timestamp(self):
        aru = self.root / "aru-hb"
        aru.mkdir(mode=0o700)
        projects_path = aru / "projects.json"
        projects_path.write_text(
            json.dumps({"schema_version": 1, "projects": {}, "migrations": {}}) + "\n",
            encoding="utf-8",
        )
        os.chmod(projects_path, 0o600)
        project_id = ap.path_derived_project_id(self.project_a)
        self.store.register(
            agent_id="cursor-old",
            family="cursor",
            project_id=project_id,
            checkout_path=str(self.project_a),
        )
        self.store.register(
            agent_id="cursor-new",
            family="cursor",
            project_id=project_id,
            checkout_path=str(self.project_a),
        )
        # Force offset-form timestamps where lexicographic order disagrees with time order.
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["agents"]["cursor-old"]["last_heartbeat"] = "2026-08-16T20:00:00+05:30"
        raw["agents"]["cursor-new"]["last_heartbeat"] = "2026-08-16T15:00:00Z"
        self.path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.path, 0o600)
        agents = {"cursor": {}, "codex": {}, "claude": {}, "antigravity": {}}
        summary = ap.doctor_presence_summary(
            project=str(self.project_a),
            agents=agents,
            store=ap.PresenceStore(self.path),
            projects_path=projects_path,
        )
        self.assertEqual(len(summary["tasks"]), 2)
        # 15:00Z == 20:30 +05:30, so Z form is newer than +05:30 form above.
        self.assertEqual(agents["cursor"]["last_heartbeat"], "2026-08-16T15:00:00Z")

    def test_doctor_summary_is_project_scoped(self):
        aru = self.root / "aru-scoped"
        aru.mkdir(mode=0o700)
        projects_path = aru / "projects.json"
        projects_path.write_text(
            json.dumps({"schema_version": 1, "projects": {}, "migrations": {}}) + "\n",
            encoding="utf-8",
        )
        os.chmod(projects_path, 0o600)
        self.store.register(
            agent_id="cursor-cloud-1",
            family="cursor",
            project_id=ap.path_derived_project_id(self.project_a),
            checkout_path=str(self.project_a),
            availability="busy",
        )
        self.store.register(
            agent_id="cursor-cloud-2",
            family="cursor",
            project_id=ap.path_derived_project_id(self.project_b),
            checkout_path=str(self.project_b),
            availability="available",
        )
        agents = {
            "cursor": {},
            "codex": {},
            "claude": {},
            "antigravity": {},
        }
        summary = ap.doctor_presence_summary(
            project=str(self.project_a),
            agents=agents,
            store=self.store,
            catalog_non_guarantees=["app quit"],
            projects_path=projects_path,
        )
        self.assertEqual(len(summary["tasks"]), 1)
        self.assertEqual(summary["tasks"][0]["agent_id"], "cursor-cloud-1")
        self.assertEqual(agents["cursor"]["last_heartbeat"], summary["tasks"][0]["last_heartbeat"])
        self.assertIn("app quit", " ".join(summary["wake_limitations"]))
        self.assertIn("GitHub claims remain authoritative", summary["ownership"])
        self.assertEqual(summary["project_id"], ap.path_derived_project_id(self.project_a))

    def test_persisted_document_is_json_object(self):
        self.store.register(
            agent_id="codex-1",
            family="openai",
            project_id="proj_alpha",
            checkout_path=str(self.project_a),
        )
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema"], ap.SCHEMA_NAME)
        self.assertIn("codex-1", raw["agents"])
        self.assertIsInstance(raw["agents"], dict)

    def test_cooldown_reason_recorded_on_register(self):
        """Register with cooldown_reason stores it on the record."""
        record = self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
            cooldown_reason="credit-exhausted",
            cooldown_until=ap._iso(self.clock["now"] + timedelta(minutes=30)),
        )
        self.assertEqual(record.cooldown_reason, "credit-exhausted")
        self.assertEqual(record.availability, "cooling-down")

    def test_cooldown_reason_recorded_on_heartbeat(self):
        """Heartbeat with cooldown_reason updates the record."""
        self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
        )
        record = self.store.heartbeat(
            "agent-1", availability="cooling-down",
            cooldown_reason="rate-limited",
            cooldown_until=ap._iso(self.clock["now"] + timedelta(minutes=5)),
        )
        self.assertEqual(record.cooldown_reason, "rate-limited")

    def test_cooldown_reason_cleared_on_availability_change(self):
        """Transitioning away from cooling-down clears cooldown_reason."""
        self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
            cooldown_reason="credit-exhausted",
        )
        record = self.store.set_availability(
            "agent-1", availability="available",
        )
        self.assertIsNone(record.cooldown_reason)

    def test_is_cooldown_expired_known_time(self):
        """is_cooldown_expired returns True when past cooldown_until."""
        now = self.clock["now"]
        record = self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
            cooldown_until=ap._iso(now - timedelta(minutes=1)),
        )
        self.assertTrue(record.is_cooldown_expired(now))

    def test_is_cooldown_expired_unknown_time(self):
        """is_cooldown_expired returns False when cooldown_until is None."""
        record = self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
        )
        self.assertFalse(record.is_cooldown_expired(self.clock["now"]))

    def test_evaluate_claim_protection_active(self):
        """Fresh heartbeat means claim is protected."""
        now = self.clock["now"]
        record = self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
        )
        result = ap.evaluate_claim_protection(record, now=now)
        self.assertTrue(result["protected"])
        self.assertEqual(result["phase"], "active")

    def test_evaluate_claim_protection_warning(self):
        """Stale heartbeat past warning but before takeover is protected+warning."""
        now = self.clock["now"]
        record = self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
        )
        # Simulate stale heartbeat by evaluating at now + 15 minutes
        future = now + timedelta(minutes=15)
        result = ap.evaluate_claim_protection(record, now=future)
        self.assertTrue(result["protected"])
        self.assertEqual(result["phase"], "warning")

    def test_evaluate_claim_protection_takeover(self):
        """Takeover requires all three explicit safety signals."""
        now = self.clock["now"]
        record = self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
        )
        future = now + timedelta(minutes=45)
        result = ap.evaluate_claim_protection(
            record,
            now=future,
            live_process=False,
            recent_branch_activity=False,
            resumable=True,
        )
        self.assertFalse(result["protected"])
        self.assertEqual(result["phase"], "takeover")

    def test_evaluate_claim_protection_fails_closed_without_each_signal(self):
        now = self.clock["now"]
        record = self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
        )
        future = now + timedelta(minutes=45)
        cases = (
            {"live_process": None, "recent_branch_activity": False, "resumable": True},
            {"live_process": False, "recent_branch_activity": None, "resumable": True},
            {"live_process": False, "recent_branch_activity": False, "resumable": None},
            {"live_process": True, "recent_branch_activity": False, "resumable": True},
            {"live_process": False, "recent_branch_activity": True, "resumable": True},
            {"live_process": False, "recent_branch_activity": False, "resumable": False},
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                result = ap.evaluate_claim_protection(record, now=future, **evidence)
                self.assertTrue(result["protected"])
                self.assertEqual(result["phase"], "warning")

    def test_evaluate_claim_protection_fails_closed_on_invalid_windows(self):
        record = self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
        )
        result = ap.evaluate_claim_protection(
            record,
            now=self.clock["now"] + timedelta(hours=1),
            warning_seconds=900,
            takeover_seconds=300,
            live_process=False,
            recent_branch_activity=False,
            resumable=True,
        )
        self.assertTrue(result["protected"])
        self.assertIn("invalid", result["reason"])

    def test_return_clears_cooldown(self):
        """Successful heartbeat after cooling-down transitions to returned and clears cooldown."""
        self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
            cooldown_reason="rate-limited",
            cooldown_until=ap._iso(self.clock["now"] + timedelta(minutes=5)),
        )
        record = self.store.heartbeat(
            "agent-1", availability="available",
        )
        self.assertEqual(record.availability, "returned")
        self.assertIsNone(record.cooldown_reason)
        self.assertIsNone(record.cooldown_until)

    def test_explicit_unavailable_heartbeat_stays_unavailable(self):
        self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
            cooldown_reason="rate-limited",
        )
        record = self.store.heartbeat("agent-1", availability="unavailable")
        self.assertEqual(record.availability, "unavailable")
        self.assertIsNone(record.cooldown_reason)
        eligible = ap.query_role_poll_agents(self.store, project_id="proj_test")
        self.assertEqual(eligible, [])

    def test_explicit_busy_heartbeat_stays_busy(self):
        self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
            cooldown_reason="rate-limited",
        )
        record = self.store.heartbeat("agent-1", availability="busy")
        self.assertEqual(record.availability, "busy")
        self.assertIsNone(record.cooldown_reason)
        eligible = ap.query_role_poll_agents(self.store, project_id="proj_test")
        self.assertEqual(eligible, [])

    def test_query_cooling_agents(self):
        """query_cooling_agents returns only cooling-down agents."""
        self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
            cooldown_reason="credit-exhausted",
        )
        self.store.register(
            agent_id="agent-2", family="anthropic",
            project_id="proj_test", checkout_path=str(self.root),
            availability="available",
        )
        cooling = ap.query_cooling_agents(self.store, project_id="proj_test")
        self.assertEqual(len(cooling), 1)
        self.assertEqual(cooling[0].agent_id, "agent-1")

    def test_role_poll_excludes_cooling_agent_and_keeps_available_peer(self):
        self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_test", checkout_path=str(self.root),
            availability="cooling-down",
            cooldown_reason="credit-exhausted",
        )
        self.store.register(
            agent_id="agent-2", family="anthropic",
            project_id="proj_test", checkout_path=str(self.root),
            availability="available",
        )
        eligible = ap.query_role_poll_agents(self.store, project_id="proj_test")
        self.assertEqual([record.agent_id for record in eligible], ["agent-2"])

    def test_project_isolation_cooldown(self):
        """Cooldown in Project A does not appear in Project B queries."""
        self.store.register(
            agent_id="agent-1", family="openai",
            project_id="proj_alpha", checkout_path=str(self.root),
            availability="cooling-down",
            cooldown_reason="credit-exhausted",
        )
        cooling_b = ap.query_cooling_agents(self.store, project_id="proj_beta")
        self.assertEqual(len(cooling_b), 0)


class FreeIdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "agent-presence.json"
        self.clock = {"now": datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)}
        self.store = ap.PresenceStore(
            self.path,
            clock=lambda: self.clock["now"],
            heartbeat_ttl_seconds=60,
        )

    def advance(self, seconds: int) -> None:
        self.clock["now"] = self.clock["now"] + timedelta(seconds=seconds)

    def test_resolves_first_free_identity(self):
        chosen = self.store.resolve_free_identity(
            ["gemini-1", "claude-1", "codex-1"], "session-a"
        )
        self.assertEqual(chosen, "gemini-1")
        # The claim is attributable to session-a.
        self.assertEqual(self.store.identity_holder("gemini-1", "session-a"), None)

    def test_two_sessions_never_get_the_same_identity(self):
        first = self.store.resolve_free_identity(
            ["gemini-1", "claude-1"], "session-a"
        )
        second = self.store.resolve_free_identity(
            ["gemini-1", "claude-1"], "session-b"
        )
        self.assertNotEqual(first, second)
        self.assertEqual(sorted((first, second)), ["claude-1", "gemini-1"])

    def test_explicit_identity_held_by_other_session_is_conflicted(self):
        self.store.resolve_free_identity(["gemini-1", "claude-1"], "session-a")
        holder = self.store.identity_holder("gemini-1", "session-b")
        self.assertEqual(holder, "session-a")

    def test_same_session_is_not_a_conflict(self):
        self.store.resolve_free_identity(["gemini-1"], "session-a")
        # Re-resolving from the same session may reclaim the same id.
        self.assertEqual(self.store.identity_holder("gemini-1", "session-a"), None)

    def test_stale_claim_is_free_again(self):
        self.store.resolve_free_identity(["gemini-1"], "session-a")
        self.advance(120)  # past the 60s TTL
        self.assertEqual(self.store.identity_holder("gemini-1", "session-b"), None)
        # And it can be handed to another session now.
        chosen = self.store.resolve_free_identity(["gemini-1"], "session-b")
        self.assertEqual(chosen, "gemini-1")

    def test_no_free_identity_raises(self):
        self.store.resolve_free_identity(["gemini-1"], "session-a")
        with self.assertRaises(ap.PresenceError):
            self.store.resolve_free_identity(["gemini-1"], "session-b")


if __name__ == "__main__":
    unittest.main()
