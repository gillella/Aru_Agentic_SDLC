import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_fleet as rf


def fleet(state="waiting", issues=1, prs=0):
    return {
        "state": state,
        "summary": f"{state}: fixture",
        "open_issues_count": issues,
        "open_prs_count": prs,
        "active_claims": [],
    }


def selection(work_type="idle", number=None):
    work = {"type": work_type}
    if number is not None:
        work["issue" if work_type == "issue" else "pr"] = number
    return {"work": work}


class FakeCommands:
    def __init__(self, statuses, selections=()):
        self.statuses = list(statuses)
        self.selections = list(selections)
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), cwd))
        script = Path(argv[1]).name
        values = self.statuses if script == "fleet_status.py" else self.selections
        if not values:
            raise AssertionError(f"unexpected command: {argv}")
        value = values.pop(0)
        if isinstance(value, rf.CommandResult):
            return value
        return rf.CommandResult(value.get("exit_code", 0), json.dumps(value), "")


class RunnerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state_dir = self.root / "state"

    def config(self, **overrides):
        values = {
            "repo": self.repo,
            "aru_home": ROOT,
            "agent": "codex-1",
            "family": "openai",
            "adapter": "codex",
            "initial_wait": 10.0,
            "max_wait": 25.0,
            "jitter": 0.0,
            "state_dir": self.state_dir,
        }
        values.update(overrides)
        return rf.RunnerConfig(**values)

    def runner(self, commands, agent_runner=lambda _argv, _cwd: 0, sleeper=lambda _seconds: None):
        return rf.FleetRunner(
            self.config(),
            command_runner=commands,
            agent_runner=agent_runner,
            sleeper=sleeper,
            random_value=lambda: 0.5,
            clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
        )


class AdapterTests(RunnerFixture):
    def test_stuck_helper_becomes_a_retryable_timeout_result(self):
        with patch.object(
            rf.subprocess, "run", side_effect=rf.subprocess.TimeoutExpired(["gh"], 3),
        ):
            result = rf.run_command(["gh", "api", "user"], self.repo, timeout=3)

        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stderr, "command timed out")

    def test_child_is_signal_isolated_and_reports_its_pid(self):
        child = unittest.mock.Mock(pid=4321)
        child.wait.return_value = 0
        started = []

        with patch.object(rf.subprocess, "Popen", return_value=child) as spawn:
            code = rf.run_agent(["agent", "one unit"], self.repo, started.append)

        self.assertEqual(code, 0)
        self.assertEqual(started, [4321])
        spawn.assert_called_once_with(
            ["agent", "one unit"], cwd=str(self.repo), start_new_session=True,
        )

    def test_custom_adapter_is_argv_only_and_prompt_stays_one_argument(self):
        config = self.config(
            adapter="auto",
            adapter_command_json=json.dumps([
                "other-agent", "--cwd", "{repo}", "--prompt", "{prompt}",
            ]),
        )
        prompt = "work; $(touch should-not-run)"

        argv = rf.build_agent_argv(config, prompt)

        self.assertEqual(argv[0], "other-agent")
        self.assertEqual(argv[2], str(self.repo))
        self.assertEqual(argv[-1], prompt)

    def test_custom_adapter_appends_prompt_when_placeholder_is_absent(self):
        config = self.config(adapter_command_json='["agent", "--batch"]')
        self.assertEqual(rf.build_agent_argv(config, "one unit"), ["agent", "--batch", "one unit"])

    def test_auto_adapter_maps_supported_families(self):
        codex = rf.build_agent_argv(self.config(adapter="auto"), "one")
        claude = rf.build_agent_argv(
            self.config(adapter="auto", family="anthropic"), "one",
        )
        self.assertEqual(codex[:2], ["codex", "exec"])
        self.assertEqual(claude[:2], ["claude", "--print"])

    def test_unknown_auto_adapter_fails_before_launch(self):
        with self.assertRaisesRegex(ValueError, "No built-in adapter"):
            rf.build_agent_argv(self.config(adapter="auto", family="google"), "one")


class IterationTests(RunnerFixture):
    def test_complete_is_a_watch_state_and_spends_no_agent_credit(self):
        commands = FakeCommands([fleet("complete", issues=0)])
        launched = []
        runner = self.runner(commands, lambda argv, cwd: launched.append((argv, cwd)) or 0)

        result = runner.run_iteration()

        self.assertEqual(result.phase, "complete_watch")
        self.assertGreater(result.delay, 0)
        self.assertEqual(launched, [])
        self.assertEqual(len(commands.calls), 1)

    def test_idle_picker_waits_without_launching_an_agent(self):
        commands = FakeCommands([fleet()], [selection()])
        launched = []
        runner = self.runner(commands, lambda argv, cwd: launched.append((argv, cwd)) or 0)

        result = runner.run_iteration()

        self.assertEqual(result.phase, "waiting")
        self.assertEqual(result.delay, 10.0)
        self.assertEqual(launched, [])

    def test_eligible_work_launches_exactly_one_finite_child(self):
        commands = FakeCommands([fleet()], [selection("issue", 45)])
        launched = []
        runner = self.runner(commands, lambda argv, cwd: launched.append((argv, cwd)) or 0)

        result = runner.run_iteration()

        self.assertEqual(result.phase, "active")
        self.assertEqual(result.work_number, 45)
        self.assertEqual(result.child_returncode, 0)
        self.assertEqual(len(launched), 1)
        self.assertIn("exactly one governed Aru Code next unit", launched[0][0][-1])
        self.assertIn("codex-1", launched[0][0][-1])

    def test_unchanged_work_after_success_waits_before_spending_again(self):
        commands = FakeCommands(
            [fleet(), fleet()],
            [selection("issue", 45), selection("issue", 45)],
        )
        launched = []
        runner = self.runner(commands, lambda argv, cwd: launched.append((argv, cwd)) or 0)

        first = runner.run_iteration()
        second = runner.run_iteration()

        self.assertEqual(first.phase, "active")
        self.assertEqual(second.phase, "waiting")
        self.assertEqual(len(launched), 1)

    def test_child_failure_becomes_recoverable_wait(self):
        commands = FakeCommands([fleet()], [selection("review", 12)])
        runner = self.runner(commands, lambda _argv, _cwd: 75)

        result = runner.run_iteration()

        self.assertEqual(result.phase, "agent_unavailable_wait")
        self.assertEqual(result.child_returncode, 75)
        self.assertGreater(result.delay, 0)
        self.assertIn(result.phase, rf.RECOVERABLE_PHASES)

    def test_picker_and_github_failures_park_instead_of_terminating(self):
        commands = FakeCommands(
            [rf.CommandResult(1, "", "network down")],
        )
        result = self.runner(commands).run_iteration()
        self.assertEqual(result.phase, "error_wait")
        self.assertGreater(result.delay, 0)

    def test_unchanged_state_uses_bounded_exponential_backoff(self):
        commands = FakeCommands(
            [fleet(), fleet(), fleet()],
            [selection(), selection(), selection()],
        )
        runner = self.runner(commands)

        delays = [runner.run_iteration().delay for _ in range(3)]

        self.assertEqual(delays, [10.0, 20.0, 25.0])

    def test_state_file_contains_metadata_not_work_or_transcript(self):
        commands = FakeCommands([fleet()], [selection("issue", 45)])
        runner = self.runner(commands)
        runner.run_iteration()

        state = runner.store.read()

        self.assertIsNotNone(state)
        self.assertEqual(
            set(state),
            {
                "version", "pid", "child_pid", "agent", "family", "repository",
                "phase", "cycle", "retry_count", "fleet_state",
                "state_fingerprint", "next_retry_at", "terminal_reason", "updated_at",
            },
        )
        serialized = json.dumps(state).lower()
        self.assertNotIn("prompt", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("title", serialized)


class LifecycleTests(RunnerFixture):
    def test_complete_loop_exits_only_after_explicit_stop_file(self):
        commands = FakeCommands([fleet("complete", issues=0)])
        runner = self.runner(commands)

        def request_stop(_seconds):
            runner.store.request_stop()

        runner.sleeper = request_stop
        code = runner.run_loop()
        state = runner.store.read()

        self.assertEqual(code, 0)
        self.assertEqual(runner.cycle, 1)
        self.assertEqual(state["phase"], "stopped")
        self.assertEqual(state["terminal_reason"], "operator_stop")

    def test_stop_requested_during_child_drains_that_child(self):
        commands = FakeCommands([fleet()], [selection("merge", 9)])
        runner = self.runner(commands)
        calls = []

        def child(_argv, _cwd):
            calls.append("started")
            runner.store.request_stop()
            calls.append("finished")
            return 0

        runner.agent_runner = child
        code = runner.run_loop()

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["started", "finished"])
        self.assertEqual(runner.store.read()["terminal_reason"], "operator_stop")

    def test_unexpected_iteration_error_parks_until_explicit_stop(self):
        def broken_commands(_argv, _cwd):
            raise RuntimeError("unexpected local failure")

        runner = self.runner(broken_commands)
        runner.sleeper = lambda _seconds: runner.store.request_stop()

        code = runner.run_loop()

        self.assertEqual(code, 0)
        self.assertEqual(runner.cycle, 1)
        self.assertEqual(runner.store.read()["terminal_reason"], "operator_stop")

    def test_metadata_write_failure_does_not_kill_the_runner(self):
        commands = FakeCommands([fleet("complete", issues=0)])
        runner = self.runner(commands)

        with patch.object(runner.store, "write", side_effect=OSError("disk unavailable")):
            result = runner.run_iteration()

        self.assertEqual(result.phase, "complete_watch")

    def test_stop_and_status_commands_share_only_control_metadata(self):
        state_dir = self.root / "cli-state"
        store = rf.StateStore(state_dir, "codex-1")
        store.write({"phase": "waiting", "cycle": 3, "fleet_state": "waiting", "retry_count": 2})

        with patch.object(rf, "validate_repo", return_value=self.repo):
            stop_code = rf.main([
                "stop", "--repo", str(self.repo), "--agent", "codex-1",
                "--state-dir", str(state_dir),
            ])
            output = io.StringIO()
            with redirect_stdout(output):
                status_code = rf.main([
                    "status", "--repo", str(self.repo), "--agent", "codex-1",
                    "--state-dir", str(state_dir), "--json",
                ])

        self.assertEqual(stop_code, 0)
        self.assertEqual(status_code, 0)
        self.assertTrue(json.loads(output.getvalue())["stop_requested"])


if __name__ == "__main__":
    unittest.main()
