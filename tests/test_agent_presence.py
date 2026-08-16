import json
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

    def test_doctor_summary_is_project_scoped(self):
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
        )
        self.assertEqual(len(summary["tasks"]), 1)
        self.assertEqual(summary["tasks"][0]["agent_id"], "cursor-cloud-1")
        self.assertEqual(agents["cursor"]["last_heartbeat"], summary["tasks"][0]["last_heartbeat"])
        self.assertIn("app quit", summary["wake_limitations"][0])
        self.assertIn("GitHub claims remain authoritative", summary["ownership"])

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


if __name__ == "__main__":
    unittest.main()
