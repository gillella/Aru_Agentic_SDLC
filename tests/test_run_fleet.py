# line-ceiling: 766
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_fleet as rf
import agent_presence as ap


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
        self.presence_path = self.root / "agent-presence.json"
        state_patcher = patch.object(
            rf, "default_state_dir", return_value=self.state_dir
        )
        presence_patcher = patch.object(
            ap, "DEFAULT_PRESENCE_PATH", self.presence_path
        )
        state_patcher.start()
        presence_patcher.start()
        self.addCleanup(state_patcher.stop)
        self.addCleanup(presence_patcher.stop)

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

    def runner(self, commands, agent_runner=lambda _argv, _cwd: 0, sleeper=lambda _seconds: None,
               presence_store=None, project_id=None, transition_notifier=None):
        return rf.FleetRunner(
            self.config(),
            command_runner=commands,
            agent_runner=agent_runner,
            sleeper=sleeper,
            random_value=lambda: 0.5,
            clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            presence_store=presence_store,
            project_id=project_id,
            transition_notifier=transition_notifier,
        )


class AdapterTests(RunnerFixture):
    def test_availability_transition_uses_project_routed_slack_helper(self):
        event = {
            "state": "cooling-down",
            "text": "rate-limited; eligibility recheck scheduled",
            "cooldown_reason": "rate-limited",
            "retry_at": "2026-08-13T12:05:00Z",
            "work_type": "issue",
            "work_number": 194,
            "dedupe_key": "availability:cycle-1:cooling-down",
        }
        with patch.object(
            rf, "run_command", return_value=rf.CommandResult(0),
        ) as run:
            rf.post_availability_transition(
                self.config(), "proj_alpha", event,
            )
        argv = run.call_args.args[0]
        self.assertIn("slack_notify.py", argv[1])
        self.assertEqual(argv[argv.index("--project-id") + 1], "proj_alpha")
        self.assertEqual(argv[argv.index("--issue") + 1], "194")
        self.assertEqual(
            argv[argv.index("--dedupe-key") + 1],
            "availability:cycle-1:cooling-down",
        )

    def test_stuck_helper_becomes_a_retryable_timeout_result(self):
        process = unittest.mock.Mock(pid=9876, returncode=None)
        process.communicate.side_effect = [
            rf.subprocess.TimeoutExpired(["gh"], 3),
            ("", ""),
        ]
        with (
            patch.object(rf.subprocess, "Popen", return_value=process),
            patch.object(rf.os, "killpg") as kill_group,
        ):
            result = rf.run_command(["gh", "api", "user"], self.repo, timeout=3)

        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stderr, "command timed out")
        kill_group.assert_called_once_with(9876, rf.signal.SIGTERM)

    def test_child_is_signal_isolated_and_reports_its_pid(self):
        child = unittest.mock.Mock(pid=4321, stderr=io.StringIO("429 rate limit\n"))
        child.wait.return_value = 75
        child.returncode = 75
        started = []

        with (
            patch.object(rf.subprocess, "Popen", return_value=child) as spawn,
            redirect_stderr(io.StringIO()),
        ):
            result = rf.run_agent(["agent", "one unit"], self.repo, started.append)

        self.assertEqual(result.returncode, 75)
        self.assertIn("rate limit", result.stderr)
        self.assertEqual(started, [4321])
        spawn.assert_called_once_with(
            ["agent", "one unit"], cwd=str(self.repo),
            stderr=rf.subprocess.PIPE, text=True, start_new_session=True,
        )

    def test_child_stderr_capture_is_a_bounded_tail(self):
        payload = "prefix-" + ("x" * rf.MAX_CHILD_STDERR_CHARS) + "-tail"
        child = unittest.mock.Mock(pid=4321, stderr=io.StringIO(payload))
        child.wait.return_value = 75
        with (
            patch.object(rf.subprocess, "Popen", return_value=child),
            redirect_stderr(io.StringIO()),
        ):
            result = rf.run_agent(["agent", "one unit"], self.repo)
        self.assertEqual(len(result.stderr), rf.MAX_CHILD_STDERR_CHARS)
        self.assertTrue(result.stderr.endswith("-tail"))

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
                "state_fingerprint", "next_retry_at", "cooldown_reason",
                "terminal_reason", "updated_at",
            },
        )
        serialized = json.dumps(state).lower()
        self.assertNotIn("prompt", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("title", serialized)

    def test_cooldown_recheck_capped_at_five_minutes(self):
        """Backoff delay for agent_unavailable_wait is capped at cooldown_recheck_seconds."""
        commands = FakeCommands([fleet("waiting")], [selection("issue", 1)])
        cfg = self.config(max_wait=900.0, cooldown_recheck_seconds=300.0)
        r = rf.FleetRunner(
            cfg,
            command_runner=commands,
            agent_runner=lambda *_: 1,
            sleeper=lambda _seconds: None,
            random_value=lambda: 0.5,
            clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
        )
        # Force high retry count to get large backoff
        r.retry_count = 20
        result = r.run_iteration()
        self.assertEqual(result.phase, "agent_unavailable_wait")
        self.assertEqual(result.delay, 300.0)

    def test_child_credit_failure_logs_cooldown_reason(self):
        """Non-zero child exit classifies failure reason in log."""
        from run_fleet import classify_child_failure
        self.assertEqual(classify_child_failure(73), "credit-exhausted")
        self.assertEqual(classify_child_failure(1, "402 billing error"), "credit-exhausted")
        self.assertEqual(classify_child_failure(75), "rate-limited")
        self.assertEqual(classify_child_failure(1, "429 rate limit exceeded"), "rate-limited")
        self.assertEqual(classify_child_failure(69), "provider-outage")
        self.assertEqual(classify_child_failure(1, "503 service unavailable"), "provider-outage")
        self.assertEqual(classify_child_failure(1), "child-crash")

    def test_failure_classifier_ignores_incidental_provider_words_and_numbers(self):
        from run_fleet import classify_child_failure

        self.assertEqual(classify_child_failure(1, "working on issue 429"), "child-crash")
        self.assertEqual(classify_child_failure(1, "credit the original author"), "child-crash")
        self.assertEqual(classify_child_failure(1, "feature unavailable in this build"), "child-crash")

    def test_child_stderr_classifies_provider_failure_in_runner(self):
        commands = FakeCommands([fleet()], [selection("issue", 1)])
        notices = []
        runner = self.runner(
            commands,
            agent_runner=lambda *_: rf.CommandResult(
                1, "", "503 service unavailable",
            ),
            transition_notifier=notices.append,
        )

        result = runner.run_iteration()

        self.assertEqual(result.phase, "agent_unavailable_wait")
        self.assertEqual(notices[0]["cooldown_reason"], "provider-outage")

    def test_repeated_failure_posts_one_transition_until_successful_requery(self):
        commands = FakeCommands(
            [fleet(), fleet(), fleet()],
            [selection("issue", 1), selection("issue", 1), selection("issue", 2)],
        )
        notices = []
        child_codes = iter((73, 73, 0))
        runner = self.runner(
            commands,
            agent_runner=lambda *_: next(child_codes),
            transition_notifier=notices.append,
        )

        runner.run_iteration()
        runner.run_iteration()
        runner.run_iteration()

        self.assertEqual([event["state"] for event in notices], ["cooling-down", "returned"])
        self.assertEqual(notices[0]["cooldown_reason"], "credit-exhausted")
        self.assertEqual(notices[1]["work_number"], 2)

    def test_reason_change_inside_one_cooldown_does_not_post_again(self):
        commands = FakeCommands(
            [fleet(), fleet(), fleet()],
            [selection("issue", 1), selection("issue", 1), selection("issue", 2)],
        )
        notices = []
        child_results = iter((
            rf.CommandResult(75, "", "429 rate limit"),
            rf.CommandResult(1, "", "generic child failure"),
            rf.CommandResult(0),
        ))
        runner = self.runner(
            commands,
            agent_runner=lambda *_: next(child_results),
            transition_notifier=notices.append,
        )

        runner.run_iteration()
        runner.run_iteration()
        runner.run_iteration()

        self.assertEqual([event["state"] for event in notices], ["cooling-down", "returned"])
        self.assertEqual(notices[0]["cooldown_reason"], "rate-limited")
        self.assertEqual(runner.active_cooldown_reason, None)

    def test_transferred_work_is_not_reused_after_cooldown(self):
        commands = FakeCommands(
            [fleet(), fleet()],
            [selection("issue", 1), selection("issue", 2)],
        )
        prompts = []
        child_codes = iter((75, 0))
        runner = self.runner(
            commands,
            agent_runner=lambda argv, _cwd: prompts.append(argv[-1]) or next(child_codes),
        )

        runner.run_iteration()
        result = runner.run_iteration()

        self.assertEqual(result.work_number, 2)
        self.assertIn("#2", prompts[-1])
        self.assertNotIn("#1", prompts[-1])


class LifecycleTests(RunnerFixture):
    def test_stop_requested_during_startup_survives_stale_marker_clear(self):
        store = rf.StateStore(self.state_dir, "codex-1")
        store.directory.mkdir(parents=True)
        store.stop_path.write_text("stale\n", encoding="utf-8")
        clear_started = threading.Event()
        request_started = threading.Event()
        original_clear = store._clear_stop_unlocked

        def delayed_clear():
            clear_started.set()
            self.assertTrue(request_started.wait(timeout=1.0))
            original_clear()

        def request_stop():
            self.assertTrue(clear_started.wait(timeout=1.0))
            request_started.set()
            store.request_stop()

        requester = threading.Thread(target=request_stop)
        requester.start()
        with patch.object(store, "_clear_stop_unlocked", side_effect=delayed_clear):
            lock = store.acquire_lock(clear_stop=True)
        requester.join(timeout=1.0)

        self.assertFalse(requester.is_alive())
        self.assertIsNotNone(lock)
        self.addCleanup(store.release_lock, lock)
        self.assertTrue(store.stop_requested())

    def test_stop_during_status_prevents_picker_and_child_launch(self):
        commands = FakeCommands([fleet()], [selection("issue", 45)])
        launched = []
        runner = self.runner(
            commands,
            lambda argv, cwd: launched.append((argv, cwd)) or 0,
        )
        original_commands = runner.command_runner

        def stop_after_status(argv, cwd):
            result = original_commands(argv, cwd)
            if Path(argv[1]).name == "fleet_status.py":
                runner.store.request_stop()
            return result

        runner.command_runner = stop_after_status
        result = runner.run_iteration()

        self.assertEqual(result.phase, "stopping")
        self.assertEqual(len(commands.calls), 1)
        self.assertEqual(launched, [])

    def test_stop_during_selection_prevents_child_launch(self):
        commands = FakeCommands([fleet()], [selection("issue", 45)])
        launched = []
        runner = self.runner(
            commands,
            lambda argv, cwd: launched.append((argv, cwd)) or 0,
        )
        original_commands = runner.command_runner

        def stop_after_selection(argv, cwd):
            result = original_commands(argv, cwd)
            if Path(argv[1]).name == "fetch_next_work.py":
                runner.store.request_stop()
            return result

        runner.command_runner = stop_after_selection
        result = runner.run_iteration()

        self.assertEqual(result.phase, "stopping")
        self.assertEqual(len(commands.calls), 2)
        self.assertEqual(launched, [])

    def test_second_runner_for_same_identity_is_refused(self):
        commands = FakeCommands([fleet("complete", issues=0)])
        first = self.runner(commands)
        second = self.runner(commands)
        lock = first.store.acquire_lock()
        self.assertIsNotNone(lock)
        self.addCleanup(first.store.release_lock, lock)

        code = second.run_loop()

        self.assertEqual(code, 2)
        self.assertEqual(second.cycle, 0)
        self.assertEqual(commands.calls, [])

    def test_once_returns_error_when_status_helper_fails(self):
        commands = FakeCommands([rf.CommandResult(1, "", "network down")])
        runner = self.runner(commands)

        code = runner.run_once()

        self.assertEqual(code, 1)
        self.assertEqual(runner.store.read()["terminal_reason"], "once_error")

    def test_once_signal_drains_child_and_restores_handlers(self):
        commands = FakeCommands([fleet()], [selection("issue", 45)])
        runner = self.runner(commands)
        installed = {}
        restored = []

        def set_handler(signum, handler):
            if handler in {"old-int", "old-term"}:
                restored.append((signum, handler))
            else:
                installed[signum] = handler

        def child(_argv, _cwd):
            installed[rf.signal.SIGTERM](rf.signal.SIGTERM, None)
            self.assertTrue(runner.stop_signal)
            return 0

        runner.agent_runner = child
        with (
            patch.object(
                rf.signal,
                "getsignal",
                side_effect=lambda signum: {
                    rf.signal.SIGINT: "old-int",
                    rf.signal.SIGTERM: "old-term",
                }[signum],
            ),
            patch.object(rf.signal, "signal", side_effect=set_handler),
        ):
            code = runner.run_once()

        self.assertEqual(code, 0)
        self.assertEqual(runner.store.read()["terminal_reason"], "operator_stop")
        self.assertEqual(
            restored,
            [(rf.signal.SIGINT, "old-int"), (rf.signal.SIGTERM, "old-term")],
        )

    def test_non_finite_timing_options_are_rejected(self):
        for option in (
            "--initial-wait", "--max-wait", "--jitter", "--helper-timeout",
        ):
            with self.subTest(option=option):
                with (
                    patch.object(rf, "validate_repo", return_value=self.repo),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    rf.main([
                        "once", "--repo", str(self.repo), "--agent", "codex-1",
                        "--family", "openai", option, "inf",
                    ])

    def test_oversized_finite_timing_options_are_rejected(self):
        for option in ("--initial-wait", "--max-wait", "--helper-timeout"):
            with self.subTest(option=option):
                with (
                    patch.object(rf, "validate_repo", return_value=self.repo),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    rf.main([
                        "once", "--repo", str(self.repo), "--agent", "codex-1",
                        "--family", "openai", option, "1e15",
                    ])

    def test_oversized_finite_jitter_is_safely_capped(self):
        self.assertEqual(
            rf.normalize_timing(
                1e15,
                "--jitter",
                minimum=0.0,
                maximum=1.0,
                cap_upper=True,
            ),
            1.0,
        )

    def test_cooldown_recheck_cli_is_capped_at_five_minutes(self):
        captured = {}

        def capture_runner(config, **_kwargs):
            captured["seconds"] = config.cooldown_recheck_seconds
            return unittest.mock.Mock(run_once=lambda: 0)

        with (
            patch.object(rf, "validate_repo", return_value=self.repo),
            patch.object(rf, "FleetRunner", side_effect=capture_runner),
        ):
            code = rf.main([
                "once", "--repo", str(self.repo), "--agent", "codex-1",
                "--family", "openai", "--cooldown-recheck", "600",
                "--project-id", "proj_test",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(captured["seconds"], 300.0)

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


class PresenceHookTests(RunnerFixture):
    def test_failure_records_reason_heartbeat_and_next_probe_in_presence_and_state(self):
        import agent_presence as ap

        clock = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        store = ap.PresenceStore(
            self.root / "presence-cooldown.json",
            clock=lambda: clock,
        )
        commands = FakeCommands([fleet()], [selection("issue", 194)])
        runner = self.runner(
            commands,
            agent_runner=lambda *_: 75,
            presence_store=store,
            project_id="proj_alpha",
        )

        runner.run_iteration()

        record = store.get("codex-1")
        self.assertEqual(record.availability, "cooling-down")
        self.assertEqual(record.cooldown_reason, "rate-limited")
        self.assertEqual(record.last_heartbeat, "2026-08-13T12:00:00Z")
        self.assertEqual(record.cooldown_until, "2026-08-13T12:00:20Z")
        state = runner.store.read()
        self.assertEqual(state["cooldown_reason"], "rate-limited")
        self.assertEqual(state["next_retry_at"], "2026-08-13T12:00:20Z")

    def test_runner_registers_and_updates_presence_without_claim_apis(self):
        import agent_presence as ap

        presence_path = self.root / "presence.json"
        store = ap.PresenceStore(
            presence_path,
            clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
        )
        commands = FakeCommands(
            [fleet("waiting"), fleet("waiting")],
            [selection("issue", 7), selection("idle")],
        )
        runner = self.runner(
            commands,
            presence_store=store,
            project_id="proj_alpha",
        )
        first = runner.run_iteration()
        self.assertEqual(first.phase, "active")
        record = store.get("codex-1")
        self.assertIsNotNone(record)
        self.assertEqual(record.project_id, "proj_alpha")
        self.assertEqual(record.availability, "available")
        self.assertEqual(record.family, "openai")
        parked = runner.run_iteration()
        self.assertEqual(parked.phase, "waiting")
        refreshed = store.get("codex-1")
        self.assertEqual(refreshed.availability, "available")
        self.assertEqual(refreshed.workload.get("phase"), "waiting")


if __name__ == "__main__":
    unittest.main()
