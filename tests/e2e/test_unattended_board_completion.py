import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import run_fleet
from fixtures.fleet import FakeLocalAgent, HelperRouter, HermeticFleet


class UnattendedBoardCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "disposable-repository"
        self.repo.mkdir()
        scenario = ROOT / "tests" / "fixtures" / "fleet" / "unattended_completion.json"
        self.fleet = HermeticFleet.from_file(scenario)
        self.router = HelperRouter(self.fleet)
        self.runners = {
            agent: self.make_runner(agent, family)
            for agent, family in self.fleet.workers.items()
        }

    def make_runner(self, agent, family, *, state_suffix=""):
        state_name = agent if not state_suffix else f"{agent}-{state_suffix}"
        config = run_fleet.RunnerConfig(
            repo=self.repo,
            aru_home=ROOT,
            agent=agent,
            family=family,
            adapter="auto",
            adapter_command_json=json.dumps(["fake-local-agent", "{prompt}"]),
            initial_wait=1.0,
            max_wait=4.0,
            jitter=0.0,
            state_dir=self.root / "state" / state_name,
        )
        return run_fleet.FleetRunner(
            config,
            command_runner=self.router,
            agent_runner=FakeLocalAgent(self.fleet, agent, family),
            sleeper=lambda _seconds: None,
            random_value=lambda: 0.5,
            clock=lambda: datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc),
        )

    def cycle(self, agent):
        return self.runners[agent].run_iteration()

    def event_index(self, event, *, issue=None):
        for index, item in enumerate(self.fleet.events):
            if item["event"] == event and (issue is None or item.get("issue") == issue):
                return index
        self.fail(f"missing event {event!r} for issue {issue!r}")

    def test_two_family_fleet_reaches_clean_done_and_stops(self):
        crash = self.cycle("codex-1")
        second = self.cycle("claude-1")

        self.assertEqual((crash.work_type, crash.work_number), ("issue", 1))
        self.assertEqual(crash.phase, "agent_unavailable_wait")
        self.assertEqual(crash.child_returncode, 75)
        self.assertEqual((second.work_type, second.work_number), ("issue", 2))
        claims = [event for event in self.fleet.events if event["event"] == "issue_claimed"]
        self.assertEqual(
            [(event["issue"], event["agent"]) for event in claims],
            [(1, "codex-1"), (2, "claude-1")],
        )
        self.assertFalse(
            set(self.fleet.issues[1].touches).intersection(self.fleet.issues[2].touches)
        )

        self.runners["codex-1"] = self.make_runner(
            "codex-1", "openai", state_suffix="restarted"
        )
        pending_review = self.cycle("codex-1")
        self.assertEqual((pending_review.work_type, pending_review.work_number), ("review", 101))
        self.fleet.advance_ci(2, "failure")
        remediation = self.cycle("claude-1")
        self.assertEqual((remediation.work_type, remediation.work_number), ("feedback", 101))
        self.assertIn(
            {"event": "ci_remediated", "pr": 101, "issue": 2, "agent": "claude-1"},
            self.fleet.events,
        )
        pending_review_wait = self.cycle("codex-1")
        self.assertEqual(pending_review_wait.phase, "waiting")
        reviewed_beta = self.cycle("codex-1")
        merged_beta = self.cycle("claude-1")
        self.assertEqual((reviewed_beta.work_type, reviewed_beta.work_number), ("review", 101))
        self.assertEqual((merged_beta.work_type, merged_beta.work_number), ("merge", 101))

        recovered = self.cycle("codex-1")
        self.assertEqual((recovered.work_type, recovered.work_number), ("issue", 1))
        self.assertEqual(self.fleet.issues[1].implementations, 1)

        continued = self.cycle("codex-1")
        self.assertEqual((continued.work_type, continued.work_number), ("issue", 4))

        third_implementation = self.cycle("codex-1")
        self.assertEqual(
            (third_implementation.work_type, third_implementation.work_number),
            ("issue", 6),
        )

        reviewed_followup = self.cycle("claude-1")
        merged_followup = self.cycle("codex-1")
        reviewed_overlap = self.cycle("claude-1")
        merged_overlap = self.cycle("codex-1")
        requested = self.cycle("claude-1")
        fixed = self.cycle("codex-1")
        review_retry_wait = self.cycle("claude-1")
        reviewed = self.cycle("claude-1")
        merged = self.cycle("codex-1")
        self.assertEqual(reviewed_followup.work_type, "review")
        self.assertEqual(reviewed_overlap.work_type, "review")
        self.assertEqual(merged_followup.work_type, "merge")
        self.assertEqual(merged_overlap.work_type, "merge")
        self.assertEqual(
            {
                event["issue"]
                for event in self.fleet.events
                if event["event"] == "review_completed" and event["issue"] in {4, 6}
            },
            {4, 6},
        )
        self.assertEqual(
            {
                event["issue"]
                for event in self.fleet.events
                if event["event"] == "merged" and event["issue"] in {4, 6}
            },
            {4, 6},
        )
        self.assertEqual(requested.work_type, "review")
        self.assertEqual(requested.work_number, 102)
        self.assertEqual(fixed.work_type, "feedback")
        self.assertEqual(review_retry_wait.phase, "waiting")
        self.assertEqual(reviewed.work_type, "review")
        self.assertEqual(merged.work_type, "merge")
        alpha_pr = next(pr for pr in self.fleet.pull_requests.values() if pr.issue == 1)
        self.assertEqual(alpha_pr.fixes, 1)
        self.assertEqual(alpha_pr.verifications, 2)
        self.assertTrue(alpha_pr.merged)
        self.assertLess(
            self.event_index("handed_off", issue=1),
            self.event_index("implemented", issue=4),
        )
        self.assertLess(
            self.event_index("implemented", issue=4),
            self.event_index("review_claimed", issue=1),
        )

        dependency_work = self.cycle("codex-1")
        self.assertEqual((dependency_work.work_type, dependency_work.work_number), ("issue", 3))

        expected_work = [
            ("claude-1", "review", 105),
            ("codex-1", "merge", 105),
        ]
        for agent, work_type, number in expected_work:
            with self.subTest(agent=agent, work_type=work_type, number=number):
                result = self.cycle(agent)
                self.assertEqual((result.work_type, result.work_number), (work_type, number))

        blocked = self.cycle("codex-1")
        self.assertEqual(blocked.phase, "blocked_wait")
        self.assertEqual(self.fleet.issues[5].status, "Blocked")
        self.assertFalse(self.fleet.human_acknowledged)

        self.fleet.acknowledge_human_gate(5)
        high_risk_flow = [
            self.cycle("codex-1"),
            self.cycle("claude-1"),
            self.cycle("codex-1"),
        ]
        self.assertEqual(
            [(item.work_type, item.work_number) for item in high_risk_flow],
            [("issue", 5), ("review", 106), ("merge", 106)],
        )

        complete = self.cycle("codex-1")
        self.assertEqual(complete.phase, "complete_watch")
        self.assertEqual(
            self.fleet.final_audit(),
            {
                "open_issues": [],
                "open_prs": [],
                "claims": [],
                "not_done": [],
                "dirty_workspaces": [],
            },
        )
        self.assertTrue(all(issue.implementations == 1 for issue in self.fleet.issues.values()))
        self.assertEqual(self.fleet.paid_adapter_launches, 0)
        self.assertEqual(self.fleet.configuration_writes, [])
        self.assertGreater(self.fleet.fake_adapter_launches, 0)

        shutdown = self.make_runner("codex-1", "openai", state_suffix="shutdown")
        shutdown.sleeper = lambda _seconds: shutdown.store.request_stop()
        self.assertEqual(shutdown.run_loop(), 0)
        shutdown_state = shutdown.store.read()
        self.assertEqual(shutdown_state["phase"], "stopped")
        self.assertEqual(shutdown_state["terminal_reason"], "operator_stop")


if __name__ == "__main__":
    unittest.main()
