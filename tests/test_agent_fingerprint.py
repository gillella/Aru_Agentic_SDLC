"""Worker-fingerprint agent identity (#310).

Ids used to come from a shared pool, assigned per process. A restart was a new
PID with no link to the worker that had been running, so it took a different
name; and two machines each arbitrating from their own local registry both took
ring[0]. Deriving the id from where the agent runs removes both.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agent_presence as ap  # noqa: E402
import fetch_next_work as fnw  # noqa: E402


class FingerprintDerivationTests(unittest.TestCase):
    def test_same_worker_resolves_to_the_same_id(self):
        first = ap.fingerprint_agent_id("anthropic", repo_root="/repo", machine="box-a")
        second = ap.fingerprint_agent_id("anthropic", repo_root="/repo", machine="box-a")
        self.assertEqual(first, second)

    def test_two_machines_resolve_to_different_ids(self):
        # The cross-machine collision: two local registries both pick ring[0].
        box_a = ap.fingerprint_agent_id("anthropic", repo_root="/repo", machine="box-a")
        box_b = ap.fingerprint_agent_id("anthropic", repo_root="/repo", machine="box-b")
        self.assertNotEqual(box_a, box_b)

    def test_two_checkouts_on_one_machine_resolve_to_different_ids(self):
        one = ap.fingerprint_agent_id("anthropic", repo_root="/repo-one", machine="box-a")
        two = ap.fingerprint_agent_id("anthropic", repo_root="/repo-two", machine="box-a")
        self.assertNotEqual(one, two)

    def test_families_are_separated(self):
        claude = ap.fingerprint_agent_id("anthropic", repo_root="/repo", machine="box-a")
        codex = ap.fingerprint_agent_id("openai", repo_root="/repo", machine="box-a")
        self.assertNotEqual(claude, codex)

    def test_id_is_readable_and_prefixed_by_product(self):
        self.assertTrue(
            ap.fingerprint_agent_id("anthropic", repo_root="/repo", machine="box-a")
            .startswith("claude-"))
        self.assertTrue(
            ap.fingerprint_agent_id("openai", repo_root="/repo", machine="box-a")
            .startswith("codex-"))

    def test_unknown_family_still_yields_a_usable_id(self):
        agent_id = ap.fingerprint_agent_id("", repo_root="/repo", machine="box-a")
        self.assertTrue(agent_id.startswith("agent-"))
        self.assertRegex(agent_id, ap.AGENT_ID_RE)

    def test_id_is_a_valid_agent_id_and_stays_short(self):
        agent_id = ap.fingerprint_agent_id("anthropic", repo_root="/repo", machine="box-a")
        self.assertRegex(agent_id, ap.AGENT_ID_RE)
        self.assertLessEqual(len(agent_id), 20)

    def test_checkout_path_is_hashed_not_embedded(self):
        # The id lands in public GitHub labels; an absolute path would name the
        # operator's home directory.
        agent_id = ap.fingerprint_agent_id(
            "anthropic", repo_root="/Users/someone/secret-project", machine="box-a")
        self.assertNotIn("someone", agent_id)
        self.assertNotIn("secret-project", agent_id)

    def test_seats_disambiguate_one_checkout(self):
        base = ap.fingerprint_agent_id("anthropic", repo_root="/repo", machine="box-a")
        seat2 = ap.fingerprint_agent_id("anthropic", repo_root="/repo", machine="box-a", seat=2)
        self.assertNotEqual(base, seat2)
        self.assertTrue(seat2.startswith(base))

    def test_env_override_is_reported(self):
        self.assertEqual(ap.configured_agent_id({"ARU_AGENT_ID": " box-agent "}), "box-agent")
        self.assertEqual(ap.configured_agent_id({}), "")


class PickerIdentityTests(unittest.TestCase):
    """The picker's end of the same behaviour."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.presence = Path(self.temporary.name) / "agent-presence.json"
        patcher = patch.object(ap, "DEFAULT_PRESENCE_PATH", self.presence)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _idle(self):
        return {"agent": "unused", "family": None,
                "work": {"type": "idle", "skill": None}, "skipped_prs": [],
                "merge_skipped": [], "claimable_issues": [],
                "mergeable_detail": [], "reviewable_detail": []}

    def _run(self, argv, env=None):
        import io
        out = patch("sys.stdout", new_callable=io.StringIO)
        err = patch("sys.stderr", new_callable=io.StringIO)
        with patch.object(fnw, "select", return_value=self._idle()) as select_mock, \
             patch.dict("os.environ", env or {}, clear=False), \
             patch("sys.argv", argv), out as _o, err as _e:
            rc = fnw.main()
        return rc, select_mock, _e.getvalue()

    def _age_out_claims(self):
        """Backdate every registry claim, the way a heartbeat gap would."""
        import json
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        document = json.loads(self.presence.read_text(encoding="utf-8"))
        for claim in (document.get("claims") or {}).values():
            claim["at"] = stale
        self.presence.write_text(json.dumps(document), encoding="utf-8")

    def test_restarted_session_gets_the_same_id_back(self):
        # A restart is a new PID with no link to the worker that was running.
        # Under the pool model it took a different name; the fingerprint is
        # recomputed from the same machine and checkout, so it comes back.
        _rc, first, _err = self._run(
            ["fetch_next_work.py", "--family", "anthropic", "--session-id", "pid-1"])
        self._age_out_claims()
        _rc2, second, _err2 = self._run(
            ["fetch_next_work.py", "--family", "anthropic", "--session-id", "pid-2"])
        self.assertEqual(first.call_args[0][0], second.call_args[0][0])

    def test_a_concurrent_second_session_gets_a_distinct_seat(self):
        argv = ["fetch_next_work.py", "--family", "anthropic", "--session-id", "pid-1"]
        _rc, first, _err = self._run(argv)
        argv2 = ["fetch_next_work.py", "--family", "anthropic", "--session-id", "pid-2"]
        _rc2, second, _err2 = self._run(argv2)
        self.assertNotEqual(first.call_args[0][0], second.call_args[0][0])

    def test_derivation_needs_no_board_query(self):
        # A restart must not depend on GitHub being reachable: a board claim
        # carrying this id is this worker's own earlier work by construction.
        with patch.object(fnw, "board_agent_identities",
                          side_effect=AssertionError("board must not be queried")):
            rc, select_mock, _err = self._run(
                ["fetch_next_work.py", "--family", "anthropic", "--session-id", "s"])
        self.assertIsNone(rc)
        self.assertTrue(select_mock.call_args[0][0].startswith("claude-"))

    def test_env_pinned_id_wins_over_derivation(self):
        rc, select_mock, _err = self._run(
            ["fetch_next_work.py", "--family", "anthropic", "--session-id", "s"],
            env={"ARU_AGENT_ID": "box-agent"})
        self.assertIsNone(rc)
        self.assertEqual(select_mock.call_args[0][0], "box-agent")

    def test_explicit_agent_still_wins_over_everything(self):
        rc, select_mock, _err = self._run(
            ["fetch_next_work.py", "--agent", "claude-1", "--session-id", "s"],
            env={"ARU_AGENT_ID": "box-agent"})
        self.assertIsNone(rc)
        self.assertEqual(select_mock.call_args[0][0], "claude-1")


if __name__ == "__main__":
    unittest.main()
