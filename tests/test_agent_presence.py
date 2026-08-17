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

    def test_doctor_summary_is_read_only(self):
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
        )
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(before, after)
        self.assertEqual(self.store.get("cursor-cloud-1").availability, "available")
        self.assertIn("Presence never launches agents", " ".join(summary["wake_limitations"]))
        self.assertIn("app quit", " ".join(summary["wake_limitations"]))

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
        self.assertIn("app quit", " ".join(summary["wake_limitations"]))
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
